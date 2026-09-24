"""The Bitcoin adapter: RPCAdapter with asset = "BTC".

Role: submodule (chain binding)
Reads / Writes / Can move funds / Mainnet-safe: identical to chains/base.py --
       this class adds no behavior, only the asset label. send_to_address()
       is inherited and CAN broadcast.

Rule 11 wants this to be where per-chain facts live -- decimals (8), the
confirmation requirement and the dust threshold -- rather than only an asset
string. They are not here yet: the confirmation requirement is in
config.Config as BTC_MIN_CONFIRMATIONS and is read from there by
services/swap_service.py, and moving it would change where a fund-moving
threshold is read from, which is the operator's call (rule 16).
"""

from .base import RPCAdapter


class BitcoinAdapter(RPCAdapter):
    asset = "BTC"
