#!/usr/bin/env python3
"""A daemon is running with no wallet loaded. Which one, and the command.

Role: submodule (a decision -- turn a -18 into a next step)
Reads: one RPC handle, passed in, for listwalletdir. It opens no socket of its
        own and knows no host, port or credential.
Writes: nothing
Can move funds: no. It NAMES loadwallet and createwallet; it calls neither.
Mainnet-safe: yes

MOVED HERE 2026-09-29 from chain_balances.py, which grew it that afternoon, when
atomic_swap.py hit the identical daemon state an hour later and CRASHED on it:

    Exception: RPC Error: {'code': -18, 'message': 'Requested wallet does not
    exist or is not loaded'}
    Traceback (most recent call last): ...

The balance reader could say which wallets were on disk and which command loads
one. The swap driver printed a stack trace about the same fact on the same
daemon. A hint that exists in one entry point and not another is the shape that
has cost this session six instances already -- see chains/daemon_conf.py's
register -- and this is the seventh, in a different dimension: not which daemon
is addressed, but what is said when it answers.

loadwallet AND createwallet ARE NAMED AND NEITHER IS CALLED. They change what the
daemon has open, and a script that quietly loads a wallet is a script an operator
cannot reason about. listwalletdir is a read.
"""

from __future__ import annotations

from pathlib import Path

# WHAT THIS CHAIN ACTUALLY HAS. See the listing_rpc check below for the wrong
# advice this removed (a `createwallet` suggestion on a daemon with no such method).
from chains import rpc_matrix
from chains.daemon_conf import CONF_FALLBACK_NETWORK
from regtest.daemons import CHAIN_DEFAULTS

# What to CALL a wallet this suggests creating. The same name the HTLC harness
# uses, so an operator who follows this hint ends up with the wallet
# regtest_htlc_verify.py will then find already loaded rather than a second one
# beside it (rule 8 -- one name for one thing).
DEFAULT_WALLET_NAME = "regtest_htlc_harness"



# A FRESHLY STARTED DAEMON HAS NO WALLET LOADED, and since Bitcoin Core 0.21 it
# does not create one either. getbalance then answers rpc code -18 with a message
# naming loadwallet and createwallet, which is most of the answer and not the part
# that says WHICH wallet -- so this asks.
#
# Matched on the message rather than the code because RPCError here is the
# repository's own wrapper and the code is not exposed as an attribute. The
# message is the daemon's and is stable across both families; the match is
# reported as a hint, never branched on for a decision.
NO_WALLET_LOADED = "No wallet is loaded"


def which_wallets_are_on_disk(adapter, chain: str) -> str:
    """The wallets this daemon could load, and the command that loads one.

    listwalletdir IS A READ. loadwallet is not -- it changes what the daemon has
    open -- so this names the command and does not run it, the same line
    what_to_do_about_it() draws around starting a daemon. A read-only balance
    reader that quietly loads a wallet is no longer a read-only balance reader,
    and tests/test_chain_balances.py holds the method list that says so.

    ADDED 2026-09-29, one layer in from the down-daemon hint and for the identical
    reason: the operator started litecoind, re-ran this twice, and got a correct
    message that did not say what to do next.
    """
    cli = CHAIN_DEFAULTS.get(chain, {}).get("cli", "")
    datadir = CHAIN_DEFAULTS.get(chain, {}).get("datadir", "")
    network = CONF_FALLBACK_NETWORK.get(chain, "")
    prefix = (f"{cli} -datadir={Path(datadir).expanduser()} -{network} " if cli and network else "")
    # A CHAIN WITH NO MULTI-WALLET RPC GETS NO MULTI-WALLET ADVICE, checked before the
    # call rather than after it fails. Added 2026-10-12, when chains/rpc_matrix.py found
    # that listwalletdir, createwallet and loadwallet are ALL absent from Gridcoin
    # 5.5.1.0 -- it has one wallet per datadir and no /wallet/<name> endpoint, which is
    # why three Gridcoin wallets on this host means three DATADIRS.
    #
    # THE OLD FALLBACK TOLD A GRC READER TO RUN `createwallet`, which that daemon does
    # not have either. That is the worst available version of this: not a missing hint
    # but a WRONG one, in a line an operator pastes, naming a command that answers
    # "Method not found". Rule 16 treats a wrong sentence with the same seriousness as
    # wrong code, and this one was reached by exactly the route the operator named --
    # the shared adapter assuming its own API is Bitcoin Core's.
    listing_rpc = rpc_matrix.cell(chain, "listwalletdir")
    if listing_rpc.state != rpc_matrix.PRESENT:
        return (
            f"{chain} has no multi-wallet RPC at all, so there is no wallet to load and nothing "
            f"to list: {listing_rpc.why} One datadir is one wallet here -- to use a different "
            f"one, point this chain's datadir elsewhere and restart that daemon."
        )
    try:
        listing = adapter.call("listwalletdir") or {}
        names = [entry.get("name", "") for entry in (listing.get("wallets") or [])]
    except Exception as error:  # noqa: BLE001 -- checked: listwalletdir is absent on a daemon built without wallet support and on older builds, and its absence costs only the names. The hint still names the two commands, so the reader is not left with nothing; the reason is printed.
        return (f"could not list this daemon's wallets ({type(error).__name__}: {error}), so which one "
                f"to load is unknown. {prefix}createwallet <a name> makes one.")
    if not names:
        # (none) is a RESULT. A daemon with an empty wallet directory needs
        # createwallet, and saying "load one of []" would be nonsense.
        return (f"this daemon has NO wallet on disk -- (none) in its wallet directory. "
                f"{prefix}createwallet {DEFAULT_WALLET_NAME} makes one, and it will be empty until "
                f"something mines or sends to it.")
    unnamed = [name or "(the unnamed default wallet)" for name in names]
    return (f"this daemon has {len(names)} wallet(s) on disk: {', '.join(unnamed)}. "
            f"{prefix}loadwallet {names[0]} loads the first.")


