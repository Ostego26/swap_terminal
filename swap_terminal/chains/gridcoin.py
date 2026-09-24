"""The Gridcoin adapter: RPCAdapter with asset = "GRC".

Role: submodule (chain binding)
Reads / Writes / Can move funds / Mainnet-safe: identical to chains/base.py --
       this class adds no behavior, only the asset label. send_to_address()
       is inherited and CAN broadcast.

Gridcoin's wallet may be encrypted, in which case `sendtoaddress` fails until
`walletpassphrase` has been called. This adapter does NOT unlock the wallet;
modules/atomic_grc_client.py does (ensure_fully_unlocked). That divergence is
noted here rather than resolved, because unlocking a wallet from the payout
path is a posture change for the operator to make deliberately.

See chains/bitcoin.py for why the per-chain constants rule 11 asks for are not
here yet.
"""

from .base import RPCAdapter


class GridcoinAdapter(RPCAdapter):
    asset = "GRC"
