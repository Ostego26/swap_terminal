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

THREE THINGS A READER MUST KNOW BEFORE CALLING THIS, ALL MEASURED 2026-09-24
AND NONE OF THEM FIXED HERE, BECAUSE ALL THREE ARE FUND MOVEMENT (rule 16).

1. THE REFUND BRANCH OF EVERY CONTRACT THIS BUILDS IS UNSPENDABLE. The locktime
   is encoded by modules/atomic_htlc_scripts.number_to_le_bytes(), which emits
   a Bitcoin VARINT (compact size), not a script number (CScriptNum). Measured
   by running it:

       locktime asked   500000
       bytes pushed     fe20a10700   (5 bytes: the 0xfe varint tag, then 4 LE bytes)
       CHECKLOCKTIMEVERIFY reads   128,000,254
       correct encoding 20a107 -> 500000

   128,000,254 is a block height roughly 127 million blocks past the tip. The
   refund path therefore never becomes spendable, so a swap whose counterparty
   walks away locks the initiator's coins permanently.

2. THE LOCKTIME IS HARDCODED TO 500000 IN ALL SIX BRANCHES BELOW, and block
   500000 is in the past on both BTC (Dec 2017) and LTC. Had the encoding been
   correct, the refund would have been claimable IMMEDIATELY, which destroys
   the atomicity the contract exists to provide. Two defects pointing in
   opposite directions; fixing either alone is worse than fixing neither.

3. `participant_address` AND `refund_address` ARE THE SAME VALUE in every
   branch -- both are the initiator's own address on the initiator's chain. The
   redeem branch and the refund branch therefore require the same key, so the
   counterparty could never claim the contract with the preimage even if the
   rest were right.

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

from modules.market_data import fetch_btc_ltc_prices, fetch_grc_price
from modules.utils import generate_secret, sha256_hash

# Configure logger
# No setLevel here: a library module that forces DEBUG decides logging policy
# for every program that imports it (rule 12, import-time side effects).
logger = logging.getLogger(__name__)

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
        logger.debug("Swapper initialized with BTC, LTC, and GRC clients.")

    def start_swap(
        self,
        swap_direction: str,
        btc_address: str,
        ltc_address: str,
        grc_address: str,
        swap_amount: Decimal
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
            swap_direction (str): The swap direction.
            btc_address (str): The Bitcoin address (used for participant and refund when applicable).
            ltc_address (str): The Litecoin address.
            grc_address (str): The Gridcoin address.
            swap_amount (Decimal): The amount of the initiator's coin to swap.
            
        Returns:
            str: A summary message with swap details, including contract details, the generated secret,
                 the computed exchange rate, and the expected receiving amount.
            
        Raises:
            Exception: If market data is unavailable.
            NotImplementedError: If the swap direction is not supported.
        """
        logger.debug(f"Starting swap: {swap_direction} with amount {swap_amount}")
        
        # Fetch market data and convert prices to Decimal.
        prices = fetch_btc_ltc_prices(force_refresh=True)
        btc_price = Decimal(str(prices.get("BTC", "0")))
        ltc_price = Decimal(str(prices.get("LTC", "0")))
        grc_price = Decimal(str(fetch_grc_price(force_refresh=True)))
        
        if btc_price == 0 or ltc_price == 0 or grc_price == 0:
            logger.error("Exchange rate not available from CoinGecko.")
            raise Exception("Exchange rate not available from CoinGecko.")
        
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
        
        exchange_rate: Decimal = Decimal(0)
        expected_amount: Decimal = Decimal(0)
        contract: Any = None
        summary: str = ""
        
        # Process different swap directions.
        if swap_direction == "BTC2LTC":
            # Initiator sends BTC, expects LTC.
            exchange_rate = ltc_price / btc_price
            expected_amount = swap_amount * exchange_rate
            contract = self.btc_client.create_contract(
                amount_btc=swap_amount,
                secret_hash=secret_hash,
                participant_address=btc_address,
                refund_address=btc_address,
                locktime=500000
            )
            summary = (
                f"BTC contract: {contract}\n"
                f"Secret: {secret.hex()}\n"
                f"Exchange Rate (BTC->LTC): {exchange_rate}\n"
                f"Expected LTC: {expected_amount}"
            )
        elif swap_direction == "LTC2BTC":
            # Initiator sends LTC, expects BTC.
            exchange_rate = btc_price / ltc_price
            expected_amount = swap_amount * exchange_rate
            contract = self.ltc_client.create_contract(
                amount_ltc=swap_amount,
                secret_hash=secret_hash,
                participant_address=ltc_address,
                refund_address=ltc_address,
                locktime=500000
            )
            summary = (
                f"LTC contract: {contract}\n"
                f"Secret: {secret.hex()}\n"
                f"Exchange Rate (LTC->BTC): {exchange_rate}\n"
                f"Expected BTC: {expected_amount}"
            )
        elif swap_direction == "BTC2GRC":
            # Initiator sends BTC, expects GRC.
            exchange_rate = grc_price / btc_price
            expected_amount = swap_amount * exchange_rate
            contract = self.btc_client.create_contract(
                amount_btc=swap_amount,
                secret_hash=secret_hash,
                participant_address=btc_address,
                refund_address=btc_address,
                locktime=500000
            )
            summary = (
                f"BTC contract: {contract}\n"
                f"Secret: {secret.hex()}\n"
                f"Exchange Rate (BTC->GRC): {exchange_rate}\n"
                f"Expected GRC: {expected_amount}"
            )
        elif swap_direction == "GRC2BTC":
            # Initiator sends GRC, expects BTC.
            exchange_rate = btc_price / grc_price
            expected_amount = swap_amount * exchange_rate
            contract = self.grc_client.create_contract(
                amount_grc=swap_amount,
                secret_hash=secret_hash,
                participant_address=grc_address,
                refund_address=grc_address,
                locktime=500000
            )
            summary = (
                f"GRC contract: {contract}\n"
                f"Secret: {secret.hex()}\n"
                f"Exchange Rate (GRC->BTC): {exchange_rate}\n"
                f"Expected BTC: {expected_amount}"
            )
        elif swap_direction == "LTC2GRC":
            # Initiator sends LTC, expects GRC.
            exchange_rate = grc_price / ltc_price
            expected_amount = swap_amount * exchange_rate
            contract = self.ltc_client.create_contract(
                amount_ltc=swap_amount,
                secret_hash=secret_hash,
                participant_address=ltc_address,
                refund_address=ltc_address,
                locktime=500000
            )
            summary = (
                f"LTC contract: {contract}\n"
                f"Secret: {secret.hex()}\n"
                f"Exchange Rate (LTC->GRC): {exchange_rate}\n"
                f"Expected GRC: {expected_amount}"
            )
        elif swap_direction == "GRC2LTC":
            # Initiator sends GRC, expects LTC.
            exchange_rate = ltc_price / grc_price
            expected_amount = swap_amount * exchange_rate
            contract = self.grc_client.create_contract(
                amount_grc=swap_amount,
                secret_hash=secret_hash,
                participant_address=grc_address,
                refund_address=grc_address,
                locktime=500000
            )
            summary = (
                f"GRC contract: {contract}\n"
                f"Secret: {secret.hex()}\n"
                f"Exchange Rate (GRC->LTC): {exchange_rate}\n"
                f"Expected LTC: {expected_amount}"
            )
        else:
            logger.error(f"Swap direction {swap_direction} is not implemented.")
            raise NotImplementedError(f"Swap direction {swap_direction} is not implemented.")
        
        logger.info("Swap initiated successfully.")
        return summary

if __name__ == "__main__":
    try:
        # Example test call (replace with real client instances):
        btc_client = None  # Replace with real BTCClient instance
        ltc_client = None  # Replace with real LTCClient instance
        grc_client = None  # Replace with real GRCClient instance
        
        swapper = Swapper(btc_client, ltc_client, grc_client)
        
        result = swapper.start_swap(
            swap_direction="BTC2LTC",
            btc_address="tb1qexampleparticipantaddress0000000000000000000000",
            ltc_address="tltc1qexampleparticipantaddress0000000000000000000000",
            grc_address="tgrc1qexampleparticipantaddress0000000000000000000000",
            swap_amount=Decimal("0.001")
        )
        logger.info(f"Swap result: {result}")
    except Exception as e:  # noqa: BLE001 -- checked: the demo driver at the bottom of the file; it logs and exits.
        logger.error(f"Error during swap: {e}")
