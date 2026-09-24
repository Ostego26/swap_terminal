"""The Bitcoin adapter: RPCAdapter with asset = "BTC".

Role: submodule (chain binding; adds no behavior, only the asset label)
Reads: a Bitcoin wallet daemon, through the methods inherited from
       chains/base.py -- validateaddress, getaddressinfo, getbalance,
       gettransaction, getrawtransaction, listtransactions
Writes: nothing to disk. THE WALLET AND THE CHAIN, through inherited methods:
       get_new_address() derives and stores a key, send_to_address()
       broadcasts.
Can move funds: YES, by inheritance. send_to_address() is RPCAdapter's and it
       calls `sendtoaddress`. The subclass adds nothing, which is exactly why
       a reader has to be told here rather than left to follow the base class.
Mainnet-safe: the read methods are; send_to_address() is the payout path.

Rule 11 wants the per-chain facts to live in these subclasses -- decimals (8
for all three Bitcoin-derived chains), the confirmation requirement and the
dust threshold -- rather than only an asset string. They are not here yet: the
confirmation requirement is in config.Config as <ASSET>_MIN_CONFIRMATIONS and
is read from there by services/swap_service.py, and moving a fund-moving
threshold to a new home is the operator's call (rule 16).
"""

from .base import RPCAdapter


class BitcoinAdapter(RPCAdapter):
    asset = "BTC"
