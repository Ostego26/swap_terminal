"""Package marker for the chain adapters.

Role: package marker (no code)
Reads: nothing
Writes: nothing
Can move funds: no -- but every module IN this package can: RPCAdapter
       inherits send_to_address(), which broadcasts.
Mainnet-safe: yes

Deliberately empty, so that importing `chains` never constructs an adapter or
reads a credential.
"""
