"""The Litecoin adapter: RPCAdapter with asset = "LTC".

Role: submodule (chain binding)
Reads / Writes / Can move funds / Mainnet-safe: identical to chains/base.py --
       this class adds no behavior, only the asset label. send_to_address()
       is inherited and CAN broadcast.

See chains/bitcoin.py for why the per-chain constants rule 11 asks for are not
here yet.
"""

from .base import RPCAdapter


class LitecoinAdapter(RPCAdapter):
    asset = "LTC"
