"""The Gridcoin adapter: RPCAdapter with asset = "GRC".

Role: submodule (chain binding; adds no behavior, only the asset label)
Reads: a Gridcoin wallet daemon, through the methods inherited from
       chains/base.py.
Writes: nothing to disk. THE WALLET AND THE CHAIN, through inherited methods.
Can move funds: YES, by inheritance -- send_to_address() calls `sendtoaddress`.
Mainnet-safe: the read methods are; send_to_address() is the payout path.

Gridcoin's wallet may be encrypted, in which case `sendtoaddress` fails until
`walletpassphrase` has been called. THIS ADAPTER STILL DOES NOT UNLOCK THE
WALLET, and that is unchanged and deliberate -- an adapter that unlocked on
every send would unlock on reads nobody audited.

WHAT CHANGED IS THAT THERE IS NOW ONE UNLOCK SEQUENCE ABOVE IT RATHER THAN TWO.
This paragraph used to record a divergence: the payout path unlocked one way and
modules/atomic_grc_client.py another (ensure_fully_unlocked), "noted here rather
than resolved". Both now go through chains/gridcoin_wallet_lock.unlocked_for_payout()
-- services/payout_service.py since 2026-09-26, the HTLC client since 2026-09-30
-- so the sequence, the timeout and the guaranteed return to STAKING are stated
once. A wallet lock is the last place two implementations should exist: the half
that gets forgotten is the restore, and forgetting it leaves an operator's wallet
not staking with nothing printed.

Rule 11 wants the per-chain facts to live in these subclasses -- decimals (8
for all three Bitcoin-derived chains), the confirmation requirement and the
dust threshold -- rather than only an asset string. They are not here yet: the
confirmation requirement is in config.Config as <ASSET>_MIN_CONFIRMATIONS and
is read from there by services/swap_service.py, and moving a fund-moving
threshold to a new home is the operator's call (rule 16).
"""

from .base import RPCAdapter


class GridcoinAdapter(RPCAdapter):
    asset = "GRC"
