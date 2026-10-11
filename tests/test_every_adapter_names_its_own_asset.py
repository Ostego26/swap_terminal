"""Every adapter the registry can build must name the chain the registry keyed it under.

Role: submodule (tests)
Reads: swap_terminal/chains/registry.py (build_adapters, called with a fully-configured
       mapping), and every adapter class it constructs
Writes: nothing
Can move funds: no. build_adapters() opens no socket -- chains/registry.py's header
       states it and its own tests hold it -- so this constructs adapters and touches
       no chain, no wallet and no network
Mainnet-safe: yes. Every url and port below is loopback and every credential is a
       fixture; nothing here is read by anything that sends

WHY IT EXISTS. services/payout_service.broadcast_payout() chooses which keywords a
chain's send needs -- XRP takes a source account, a seed and an arming token, SOL
takes an arming token, ICP takes an idempotency key, and BTC/LTC/GRC take two
positional arguments because the daemon holds the wallet. Until 2026-10-11 it took
the asset as a SEPARATE argument beside the adapter, so the two could disagree and
nothing but the caller's discipline stopped a Gridcoin daemon being handed an ICRC-1
idempotency key. It now derives the asset from `adapter.asset`.

THAT DERIVATION IS ONLY SOUND IF THIS HOLDS, which is why the assertion is here and
not a comment there. Measured when the change was made: six of the seven adapter
modules declared `asset`, and the one that did not was chains/icp.py -- the only
adapter not subclassing base.RPCAdapter, where the attribute is declared. ICP was
therefore the chain whose payout the derivation would have silently sent down the
two-positional branch with no idempotency key, which is the exact defect the change
was made to fix.

AND IT IS THE KEY THAT MATTERS, NOT MERELY THE PRESENCE. An adapter carrying
`asset = "SOL"` while the registry files it under "USDC" would route a token payout
through the native-SOL branch. There is no such adapter today -- the SPL mint is a
SETTING inside the SOL adapter rather than an adapter of its own, measured by reading
every `adapters[...] =` in build_adapters() and finding six keys -- and this is what
would fail the day one is added without a matching `asset`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from chains.registry import build_adapters  # noqa: E402

#: Not a real credential. Named rather than inline so a secret scanner reading this
#: file finds a constant whose name says what it is.
RPC_FIXTURE_AUTH = "fixture-not-a-credential"

#: Enough configuration for build_adapters() to construct EVERY adapter it knows how
#: to build. Loopback everywhere, and ICP included -- which is the entry
#: tests/test_address_authority.py's own fully-configured mapping does not carry, so
#: that one builds five adapters rather than the six its comment used to claim.
FULLY_CONFIGURED = {
    "BTC": {"user": "u", "password": RPC_FIXTURE_AUTH, "host": "127.0.0.1", "port": 18443},
    "LTC": {"user": "u", "password": RPC_FIXTURE_AUTH, "host": "127.0.0.1", "port": 19443},
    "GRC": {"user": "u", "password": RPC_FIXTURE_AUTH, "host": "127.0.0.1", "port": 25715},
    "SOL": {"url": "http://127.0.0.1:8899"},
    "XRP": {"url": "http://127.0.0.1:5005"},
    "ICP": {
        "ledger_canister_id": "ryjl3-tyaaa-aaaaa-aaaba-cai",
        "owner_principal": "2vxsx-fae",
    },
}


def test_every_adapter_the_registry_builds_names_its_own_asset():
    """`adapter.asset` must equal the key build_adapters() filed it under, for all of them.

    MUTATION: delete `asset = "ICP"` from chains/icp.ICPAdapter and this fails naming
    ICP with an empty string -- which is precisely the state in which
    broadcast_payout() would have paid an ICP swap through the BTC branch.
    """
    adapters = build_adapters(FULLY_CONFIGURED)
    assert adapters, "build_adapters() built nothing from a fully-configured mapping"

    disagreeing = {
        key: getattr(adapter, "asset", None)
        for key, adapter in adapters.items()
        if getattr(adapter, "asset", None) != key
    }
    assert disagreeing == {}, (
        f"these adapters do not name the chain they are keyed under: {disagreeing}. "
        "services/payout_service.broadcast_payout() derives the asset from the adapter, so a "
        "disagreement here is a payout sent down another chain's branch -- unsigned for XRP, "
        "unarmed for SOL, with no idempotency key for ICP, and silently so in every case."
    )


def test_the_fully_configured_mapping_actually_reaches_every_chain():
    """The mapping above must build an adapter for EVERY key it configures.

    Without this, the assertion above passes vacuously the day a chain stops being
    constructible: build_adapters() skips a chain whose settings are incomplete, so a
    typo in this fixture would quietly shrink the set under test rather than fail it.
    That is the shape of defect CLAUDE.md rule 3 is about -- a count with no
    denominator -- and the denominator here is the configured keys.
    """
    adapters = build_adapters(FULLY_CONFIGURED)
    missing = sorted(set(FULLY_CONFIGURED) - set(adapters))
    assert missing == [], (
        f"{missing} is configured above and build_adapters() built no adapter for it, so "
        "test_every_adapter_the_registry_builds_names_its_own_asset is not checking it"
    )
