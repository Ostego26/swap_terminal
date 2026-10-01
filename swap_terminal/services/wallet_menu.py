"""Which wallets a deposit can be paid from, and which of them can sign for the chain.

Role: submodule -> function (wallets_for() is the decision; the catalog is data)
Reads: nothing. The asset is an argument. No environment, no file, no socket.
Writes: nothing
Can move funds: no. It names wallets. A wallet moves funds when its owner
      approves, and nothing here signs, sends or holds a key.
Mainnet-safe: yes

WHY A CATALOG AND NOT THREE BUTTONS IN A TEMPLATE.

Asked for by the operator 2026-10-01: "creata a clicking submenu of wallets of
coinbase, metamask, and phatom."

METAMASK IS NOT HERE, AND THAT IS THE RECORD OF WHY. It cannot send native SOL:
it is an EVM wallet, its accounts are secp256k1 and Solana's are ed25519, and no
amount of UI makes one sign for the other. It was briefly listed as a disabled
entry that explained itself, and the operator's answer to that was "forget
metamask then" -- so it is gone rather than greyed out. Written down because the
request named it, and somebody reading that request later should find the reason
here instead of adding it back. (MetaMask Snaps can add Solana support; if that
becomes standard it is one Wallet(...) entry.)

STATED AS A BELIEF WITH A REASON, NOT A MEASUREMENT (rule 17): this container has
no browser and no extension, so what is written here about each wallet's Solana
support is read off the chains' key types and the wallets' documented scope, not
observed. The page detects what is actually INSTALLED at runtime; this catalog
only says what each wallet could sign for if it were.

A wallet that supports SOME chain but not the deposit's is still listed and
still explains itself -- Phantom on a GRC swap says so -- because a customer who
has a wallet installed and sees no entry for it concludes the page is broken.

WHAT THE PAGE ADDS THAT THIS CANNOT.

Whether a wallet is INSTALLED is only knowable in the browser, so each entry
carries the global the page probes for -- `window.phantom.solana` and so on --
and static/script.js enables an entry when the probe finds a provider. The
catalog is the authority on CAPABILITY; the browser is the authority on
PRESENCE. Keeping those apart is what stops the page from deciding, in
JavaScript the server cannot check, which chains a wallet supports.
"""

from __future__ import annotations

from typing import NamedTuple


class Wallet(NamedTuple):
    """One wallet, what it can sign for, and how a page finds it.

    `provider_path` is a dotted path from `window`, resolved by
    static/script.js. A STRING rather than a function so the catalog stays data
    the server can test and the browser cannot reinterpret -- a callable here
    would be logic in two languages, which is the shape rule 8 is about.

    `install_url` is where a customer without it goes. Rendered as a link only
    when the probe did NOT find the wallet, so the page never advertises at
    somebody who already has it.
    """

    key: str
    name: str
    chains: frozenset[str]
    provider_path: str
    install_url: str
    note: str


#: The three the operator named, plus Solflare, and each `chains` entry is the
#: claim this module is accountable for.
#:
#: Solflare is included because it is the other mainstream Solana browser wallet
#: and leaving it out would make the menu a list of what was asked for rather
#: than a list of what works -- the same reason chains/registry.py builds every
#: configured chain instead of the ones somebody remembered.
WALLETS = (
    Wallet(
        key="phantom",
        name="Phantom",
        chains=frozenset({"SOL"}),
        provider_path="phantom.solana",
        install_url="https://phantom.app/download",
        note="a Solana wallet; it signs ed25519 and can hold SOL",
    ),
    Wallet(
        key="solflare",
        name="Solflare",
        chains=frozenset({"SOL"}),
        provider_path="solflare",
        install_url="https://solflare.com/download",
        note="a Solana wallet; it signs ed25519 and can hold SOL",
    ),
    Wallet(
        key="coinbase",
        name="Coinbase Wallet",
        chains=frozenset({"SOL"}),
        provider_path="coinbaseSolana",
        install_url="https://www.coinbase.com/wallet/downloads",
        note="the self-custody Coinbase Wallet extension, which supports Solana. NOT a "
             "coinbase.com account -- an exchange account has no browser provider and cannot "
             "be asked to sign anything from a page",
    ),
)


class MenuEntry(NamedTuple):
    """A wallet as the page should render it for one particular deposit."""

    wallet: Wallet
    can_sign: bool
    why_not: str


def wallets_for(asset: str) -> list[MenuEntry]:
    """Every wallet, marked for whether it can sign a deposit on `asset`. THE DECISION.

    RETURNS ALL OF THEM, and the ordering puts the capable ones first. Filtering
    the incapable ones out was the first design and it is worse: a customer with
    MetaMask installed and no MetaMask entry has no way to learn that MetaMask
    is not the problem, and will try to make it work.

    `why_not` is per-wallet and per-asset rather than one generic sentence,
    because "MetaMask cannot hold SOL" and "this wallet does not support BTC"
    are different facts and the second one is the honest answer for a chain
    nobody has wired a browser wallet for.
    """
    asset = (asset or "").strip().upper()
    entries = []
    for wallet in WALLETS:
        can_sign = asset in wallet.chains
        if can_sign:
            why_not = ""
        else:
            supported = ", ".join(sorted(wallet.chains))
            why_not = (
                f"{wallet.name} signs for {supported}, not {asset or 'this chain'}, so it cannot pay "
                f"this deposit"
            )
        entries.append(MenuEntry(wallet=wallet, can_sign=can_sign, why_not=why_not))
    return sorted(entries, key=lambda entry: (not entry.can_sign, entry.wallet.name))


def any_can_sign(asset: str) -> bool:
    """Whether a wallet menu is worth rendering at all for this asset.

    A panel offering four wallets that all say "cannot pay this deposit" is
    worse than no panel: it reads as a broken feature rather than as a chain
    nobody has wired one for. The address and the QR stand on their own.
    """
    return any(entry.can_sign for entry in wallets_for(asset))
