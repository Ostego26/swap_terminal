"""Package marker for the atomic-swap modules.

Role: package marker (no code)
Reads: nothing
Writes: nothing
Can move funds: no -- but modules IN this package do: the three
       atomic_*_client.py files create HTLC contracts, sign and broadcast.
Mainnet-safe: yes

Deliberately empty. Note that importing most of this package is NOT free:
modules/atomic_htlc_scripts.py raises at import time if SECRET_HASH is unset,
and modules/market_data.py used to require the undeclared `bitshares` package.
Both are noted in those files.
"""
