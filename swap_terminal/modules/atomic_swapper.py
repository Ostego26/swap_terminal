#!/usr/bin/env python3
"""Initiate an atomic swap: generate the secret, fund the initiator's HTLC.

Role: module (orchestration over the three atomic_*_client submodules)
Reads: CoinGecko through modules/market_data.py
Writes: nothing to disk. THE CHAIN: start_swap() calls create_contract() on
       one of the three clients, which sends funds to a P2SH address.
Can move funds: YES, indirectly and irreversibly. Every branch below funds an
       HTLC on the initiator's chain.
Mainnet-safe: NO. Calling start_swap() locks real coins in a contract whose
       refund branch is measured below to be unspendable.

THREE MEASURED DEFECTS, ALL THREE FIXED ON 2026-09-24 IN ONE COMMIT, BECAUSE
FIXING ANY ONE OF THEM ALONE WOULD HAVE LEFT THE SYSTEM WORSE THAN FINDING
ALL THREE.

1. THE REFUND BRANCH OF EVERY CONTRACT THIS BUILT WAS UNSPENDABLE. The locktime
   was encoded by modules/atomic_htlc_scripts.number_to_le_bytes(), which
   emitted a Bitcoin VARINT (compact size), not a script number (CScriptNum).
   Measured by running it:

       locktime asked   500000
       bytes pushed     fe20a10700   (5 bytes: the 0xfe varint tag, then 4 LE bytes)
       CHECKLOCKTIMEVERIFY read     128,000,254
       correct encoding 20a107 -> 500000

   128,000,254 is a block height roughly 127 million blocks past the tip, so
   the refund path never became spendable and a swap whose counterparty walked
   away locked the initiator's coins permanently. encode_script_number()
   replaces that encoder.

2. THE LOCKTIME WAS HARDCODED TO 500000 IN ALL SIX BRANCHES, and block 500000
   is in the past on both BTC (Dec 2017) and LTC. With the encoding corrected
   and the literal left alone, the refund would have been claimable
   IMMEDIATELY on funding -- the initiator refunds their own leg and still
   redeems the counterparty's, taking both. Two defects pointing in opposite
   directions, which is exactly why they could not be fixed separately. The
   locktime now comes from modules/htlc_timelock.contract_locktime(), derived
   from the funded chain's own tip, with the INITIATOR's longer lock because
   start_swap() funds the first leg by definition.

3. `participant_address` AND `refund_address` WERE THE SAME VALUE in every
   branch -- both the initiator's own address on the initiator's chain -- so
   the redeem branch and the refund branch needed the same key and the
   counterparty could never claim with the preimage. They are separate required
   parameters now, and build_htlc_redeem_script() refuses a script whose two
   branches resolve to one hash160.

WHAT IS STILL UNPROVEN (rule 17): nothing built by this module has been funded,
redeemed or refunded on any chain from here, because this machine has no chain
access. The encoding, the timelock arithmetic and the address split are proven
by tests over the real functions' real output; a spend is not. The refund
branch in particular has never been exercised, and it is the branch that runs
when something has already gone wrong.

WHAT ELSE THE SIX-BRANCH MERGE SURFACED, AND WAS NOT CHANGED. The exchange rate
is computed as price[to] / price[from] and the expected amount as
amount * rate, identically in all six directions before the merge and after
it. That reads backwards -- one BTC at $100,000 against LTC at $100 reports
`Expected LTC: 0.001` where the counterparty would owe roughly 1000 LTC. It is
a QUOTED figure shown to the operator and sizes no transaction, but it is the
number a human judges the swap by, so it is surfaced rather than re-derived
here (rule 16: an amount is the operator's call).

The six near-identical `elif` branches that used to be here are gone: one
table-driven path builds every direction, so a change to how a contract is
funded is a change in one place rather than six that agree today (rule 8). The
`if __name__ == "__main__":` demo at the bottom went with them -- it built a
Swapper from three `None` clients and called start_swap() with the old
per-chain address arguments, so it could never have run; this is a module, and
entry points live at the root (rule 10).

The `secret` is deliberately still returned inside `summary`: the initiator
needs the preimage to redeem the counterparty's leg, and there is no other
channel to hand it over. What was removed is the logger.info() that ALSO wrote
it into the log stream -- see the comment at that site.

Description (original):
  This module defines the unified Swapper class used to initiate atomic swaps.
  It implements six swap directions between BTC, LTC, and GRC.
  
  When "Swap" is pressed, the current exchange rates are fetched from CoinGecko
  (forcing a refresh) and used to compute the expected counterparty amount.
  
  A random secret is generated and its SHA-256 hash is used in the HTLC contract
  created on the initiator's chain.
  
  Supported swap directions:
    - BTC2LTC: Initiator sends BTC, expects LTC.
    - LTC2BTC: Initiator sends LTC, expects BTC.
    - BTC2GRC: Initiator sends BTC, expects GRC.
    - GRC2BTC: Initiator sends GRC, expects BTC.
    - LTC2GRC: Initiator sends LTC, expects GRC.
    - GRC2LTC: Initiator sends GRC, expects LTC.
"""

import logging
from decimal import Decimal
from typing import Any

from modules.htlc_timelock import ROLE_INITIATOR, contract_locktime, describe_locktime
from modules.market_data import fetch_btc_ltc_prices, fetch_grc_price
from modules.utils import generate_secret, sha256_hash

# Configure logger
# No setLevel here: a library module that forces DEBUG decides logging policy
# for every program that imports it (rule 12, import-time side effects).
logger = logging.getLogger(__name__)

# Which keyword each client's create_contract() takes the amount under. The
# three clients disagree about this (and about the ORDER of their parameters --
# see the divergence table in any of modules/atomic_*_client.py), so the name
# is looked up here rather than spelled six times. Every call below passes
# every argument by KEYWORD, which is what makes LTC's different parameter
# order harmless: passing positionally in the BTC/GRC order would hand LTC the
# secret hash as its participant address.
_AMOUNT_KWARG = {
    "BTC": "amount_btc",
    "LTC": "amount_ltc",
    "GRC": "amount_grc",
}

SUPPORTED_ASSETS = ("BTC", "LTC", "GRC")


class Swapper:
    def __init__(self, btc_client: Any, ltc_client: Any, grc_client: Any) -> None:
        """
        Initialize the Swapper with clients for Bitcoin, Litecoin, and Gridcoin.
        
        Args:
            btc_client: The Bitcoin client instance.
            ltc_client: The Litecoin client instance.
            grc_client: The Gridcoin client instance.
        """
        self.btc_client = btc_client
        self.ltc_client = ltc_client
        self.grc_client = grc_client
        self.clients = {"BTC": btc_client, "LTC": ltc_client, "GRC": grc_client}
        logger.debug("Swapper initialized with BTC, LTC, and GRC clients.")

    def chain_tip(self, asset: str) -> int:
        """Current block height of `asset`'s chain, from its own daemon.

        This is the input the locktime is derived from, so it is fetched from
        the chain being FUNDED and from nothing else: a height borrowed from
        another chain would produce a locktime that is meaningless on this one.

        No fallback. If `getblockcount` fails the exception propagates and no
        contract is built -- because the alternative, a default height, is the
        defect this whole change exists to remove. A swap that cannot learn the
        tip must not fund an HTLC with a guessed timelock.

        `getblockcount` is a standard Bitcoin JSON-RPC method and Gridcoin is a
        Bitcoin derivative that exposes it; that it answers on all three of the
        operator's daemons has NOT been checked from here (rule 17), because
        this machine has no chain access. If a daemon does not implement it,
        this raises with the RPC error in the message rather than proceeding.
        """
        height = int(self.clients[asset].rpc_call("getblockcount"))
        logger.info(f"{asset} chain tip: {height} (block height, never converted to microfortnights -- rule 6)")
        return height

    def start_swap(
        self,
        swap_direction: str,
        participant_address: str,
        refund_address: str,
        swap_amount: Decimal,
    ) -> str:
        """
        Initiate an atomic swap based on the specified direction.
        
        Supported directions:
          - BTC2LTC: Initiator sends BTC, expects LTC.
          - LTC2BTC: Initiator sends LTC, expects BTC.
          - BTC2GRC: Initiator sends BTC, expects GRC.
          - GRC2BTC: Initiator sends GRC, expects BTC.
          - LTC2GRC: Initiator sends LTC, expects GRC.
          - GRC2LTC: Initiator sends GRC, expects LTC.

        Args:
            swap_direction (str): The swap direction, "<FROM>2<TO>".
            participant_address (str): THE COUNTERPARTY'S address, on the chain
                being funded (the FROM chain). Whoever holds its key can take
                the coins by revealing the preimage. This used to be the
                initiator's own address -- see the note below.
            refund_address (str): YOUR address, on the same funded chain.
                Whoever holds its key can take the coins back after the
                timelock expires.
            swap_amount (Decimal): The amount of the initiator's coin to swap.

        Returns:
            str: A summary message with swap details, including contract details, the generated secret,
                 the computed exchange rate, and the expected receiving amount.
            
        Raises:
            ValueError: If the direction is malformed, names an unsupported
                asset, or if the two addresses are the same string.
            Exception: If market data is unavailable.
            NotImplementedError: If the swap direction is not supported.

        THE TWO ADDRESSES USED TO BE ONE VALUE, AND THAT WAS THE THIRD DEFECT.

        Until 2026-09-24 every one of the six branches passed the initiator's
        own address as BOTH `participant_address` and `refund_address`, so both
        branches of the HTLC required the same key and the counterparty could
        never redeem with the preimage. They are separate parameters now, and
        build_htlc_redeem_script() refuses to build a script whose two branches
        resolve to the same hash160 -- a guard at the function that decides,
        rather than a convention at the six call sites that used to exist here.

        The locktime is no longer a literal either. It comes from
        modules/htlc_timelock.contract_locktime() with the chain's own tip and
        the INITIATOR's (longer) lock, because start_swap funds the first leg
        of the swap by definition. The participant's shorter lock is for
        whoever builds the responding leg.
        """
        logger.debug(f"Starting swap: {swap_direction} with amount {swap_amount}")

        from_asset, _, to_asset = swap_direction.partition("2")
        if from_asset not in SUPPORTED_ASSETS or to_asset not in SUPPORTED_ASSETS or from_asset == to_asset:
            logger.error(f"Swap direction {swap_direction} is not implemented.")
            raise NotImplementedError(f"Swap direction {swap_direction} is not implemented.")
        if participant_address.strip() == refund_address.strip():
            raise ValueError(
                "participant_address and refund_address are the same value. The participant address is the "
                "COUNTERPARTY's address on the funded chain; the refund address is YOURS. If they match, the "
                "counterparty cannot redeem with the preimage and the contract only pays you back."
            )

        # Fetch market data and convert prices to Decimal.
        prices = fetch_btc_ltc_prices(force_refresh=True)
        btc_price = Decimal(str(prices.get("BTC", "0")))
        ltc_price = Decimal(str(prices.get("LTC", "0")))
        grc_price = Decimal(str(fetch_grc_price(force_refresh=True)))
        
        if btc_price == 0 or ltc_price == 0 or grc_price == 0:
            logger.error("Exchange rate not available from CoinGecko.")
            raise Exception("Exchange rate not available from CoinGecko.")

        price = {"BTC": btc_price, "LTC": ltc_price, "GRC": grc_price}

        # Generate a random secret and compute its SHA-256 hash.
        secret = generate_secret(32)
        secret_hash = sha256_hash(secret).hex()
        # The preimage is NEVER logged. It used to be, at INFO, one line above
        # the hash -- so every swap this function started published the value
        # that spends both legs of it into the log stream. The hash is the
        # value that goes into the redeem script and is public by
        # construction, so it identifies the swap without being able to spend
        # it. The preimage still travels to the initiator in `summary` below,
        # because they need it to redeem; a log file is not that channel.
        logger.info(f"Secret hash: {secret_hash}")

        # MEASURED AND NOT CHANGED (rule 16: an amount is the operator's call).
        # This is the arithmetic the six branches carried before they were
        # merged into one, preserved exactly: rate = price[to] / price[from],
        # and expected = amount * rate. That reads backwards -- one BTC at
        # $100,000 against LTC at $100 yields `Exchange Rate (BTC->LTC): 0.001`
        # and `Expected LTC: 0.001` for a whole bitcoin, where the counterparty
        # would owe roughly 1000 LTC. All six directions were inverted the same
        # way, so this is not a merge artifact. It is a QUOTED amount shown to
        # the operator and is not used to size any transaction -- the contract
        # funds `swap_amount` of the initiator's own coin -- but it is the
        # number a human judges the swap by, which is why it is reported rather
        # than silently re-derived here.
        exchange_rate: Decimal = price[to_asset] / price[from_asset]
        expected_amount: Decimal = swap_amount * exchange_rate

        # One locktime, derived from the funded chain's own tip, for the
        # INITIATOR's leg. Announced before the contract is created rather than
        # after (rule 14): creating it broadcasts.
        tip = self.chain_tip(from_asset)
        locktime = contract_locktime(from_asset, ROLE_INITIATOR, tip)
        logger.info(describe_locktime(from_asset, ROLE_INITIATOR, tip, locktime))
        logger.info(
            f"funding {swap_amount} {from_asset} into an HTLC: participant={participant_address} (counterparty), "
            f"refund={refund_address} (yours). This BROADCASTS."
        )

        contract = self.clients[from_asset].create_contract(
            secret_hash=secret_hash,
            participant_address=participant_address,
            refund_address=refund_address,
            locktime=locktime,
            **{_AMOUNT_KWARG[from_asset]: swap_amount},
        )

        summary = (
            f"{from_asset} contract: {contract}\n"
            f"Secret: {secret.hex()}\n"
            f"Locktime: {locktime} ({from_asset} block height, tip was {tip})\n"
            f"Exchange Rate ({from_asset}->{to_asset}): {exchange_rate}\n"
            f"Expected {to_asset}: {expected_amount}"
        )

        logger.info("Swap initiated successfully.")
        return summary
