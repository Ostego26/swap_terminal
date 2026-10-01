"""The wallet submenu, the QR, and the payment request the page offers.

Role: test (pure functions plus a rendered page; opens no socket)
Reads: services/wallet_menu.py, swap_terminal/qr_svg.py,
       services/swap_view.payment_options()
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- a Solana Pay URI names no cluster and a QR is a picture of
        one, so nothing here can select a network or reach it.

WHAT THESE TESTS CANNOT ESTABLISH, said rather than implied (rule 17): whether
Phantom, Solflare or Coinbase Wallet actually honour a `solana:` request when
clicked. This container has no browser and no extension. They assert what the
SERVER produces -- the capability table, the request string, the SVG -- and the
operator's machine is the only place the extension behaviour can be observed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

import qr_svg
from services.swap_view import payment_options
from services.wallet_menu import WALLETS, any_can_sign, wallets_for

ACCOUNT = "CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp"


def sol_swap(**overrides) -> dict:
    swap = {
        "id": "s_pay", "from_asset": "SOL", "to_asset": "GRC",
        "deposit_address": ACCOUNT, "deposit_tag": 7,
        "expected_input_amount": 0.01, "status": "awaiting_deposit",
    }
    swap.update(overrides)
    return swap


# --- the catalog --------------------------------------------------------------

def test_metamask_is_not_in_the_catalog():
    """Removed at the operator's request, and pinned so it does not come back.

    "forget metamask then", 2026-10-01, after it was briefly listed as a disabled
    entry. It cannot send native SOL -- an EVM wallet signs secp256k1 and Solana
    needs ed25519 -- so an entry for it could only ever be a greyed-out row.

    The request that started this feature NAMED MetaMask, so somebody reading
    that request later will wonder. This test and the module docstring are where
    they find the answer.
    """
    assert "metamask" not in {wallet.key for wallet in WALLETS}
    assert not any("MetaMask" in wallet.name for wallet in WALLETS)


def test_every_wallet_in_the_catalog_can_sign_for_sol():
    """The whole menu is capable, which is what makes it worth rendering.

    After MetaMask came out, no catalog entry has an empty `chains` -- so
    wallets_for()'s "cannot sign for any chain" branch became unreachable and was
    removed with it (rule 9). This asserts the condition that made it dead, so a
    future entry with no chains fails here rather than silently hitting a branch
    that no longer exists.
    """
    for wallet in WALLETS:
        assert wallet.chains, f"{wallet.name} supports no chains; the branch for that case is gone"
        assert "SOL" in wallet.chains


def test_a_wallet_that_cannot_sign_for_the_chain_is_listed_with_its_reason():
    """Listed, not hidden. A customer with the wallet installed and no entry for it
    concludes the page is broken and goes on trying."""
    entries = wallets_for("GRC")

    assert len(entries) == len(WALLETS), "every wallet is listed for every asset"
    assert all(not entry.can_sign for entry in entries)
    for entry in entries:
        assert "not GRC" in entry.why_not
        assert entry.wallet.name in entry.why_not, "the reason names the wallet"


def test_no_menu_is_offered_for_a_chain_no_wallet_can_pay():
    """A panel of four "cannot pay this deposit" rows reads as a broken feature.

    The address and the QR stand on their own, so the menu simply is not rendered.
    """
    assert any_can_sign("SOL") is True
    assert any_can_sign("GRC") is False
    assert any_can_sign("") is False


def test_the_capable_wallets_come_first():
    entries = wallets_for("SOL")
    capable = [entry.can_sign for entry in entries]
    assert capable == sorted(capable, reverse=True), "capable entries lead the list"


# --- the QR -------------------------------------------------------------------

def test_the_qr_is_inline_svg_with_no_xml_declaration():
    """It is embedded in an HTML document, not served as a file.

    A declaration inside HTML is markup the parser has to recover from.
    """
    svg = qr_svg.render("solana:" + ACCOUNT + "?amount=0.01&memo=7")

    assert svg is not None
    assert svg.startswith("<svg")
    assert "<?xml" not in svg
    assert "viewBox" in svg


def test_the_qr_encodes_the_payload_it_was_given_and_nothing_else():
    """Two different requests must not draw the same symbol.

    The point of drawing server-side is that the bytes scanned are the bytes
    built. A renderer that ignored its argument -- or cached one symbol -- would
    pass every structural check above and send every customer to one address.
    """
    first = qr_svg.render("solana:" + ACCOUNT + "?amount=0.01&memo=7")
    second = qr_svg.render("solana:" + ACCOUNT + "?amount=0.01&memo=8")

    assert first != second, "a different memo must draw a different symbol"


def test_an_empty_payload_draws_nothing_rather_than_raising():
    """A caller with nothing to encode has nothing to show, and the page says why.

    Raising would turn a missing value into a 500 on the one page a customer
    needs in order to be paid.
    """
    assert qr_svg.render("") is None


def test_the_unavailable_reason_says_what_to_install_and_that_it_is_optional():
    reason = qr_svg.unavailable_reason()

    assert "segno" in reason
    assert "complete without it" in reason, "the instruction stands without a QR"


# --- what the page is handed ---------------------------------------------------

def test_payment_options_builds_the_request_the_qr_and_the_menu_together():
    """One function, so the three cannot disagree about where money goes.

    The URI is what the QR encodes AND what a wallet is handed; two builders
    would be rule 8's duplicate on exactly that string.
    """
    pay = payment_options(sol_swap())

    assert pay["uri"] == f"solana:{ACCOUNT}?amount=0.01&memo=7&label=swap_terminal"
    assert pay["qr"] is not None
    assert pay["show_wallets"] is True
    assert [entry.wallet.key for entry in pay["wallets"]][:1] != [], "the menu is populated"


@pytest.mark.parametrize("status", ["payout_pending", "completed", "failed", "under_review"])
def test_a_closed_swap_is_offered_no_request_and_no_qr(status):
    """A scannable code is the most live-looking deposit target there is.

    templates/swap.html already refuses to show a live-looking target for a
    closed swap -- found 2026-09-26 by looking at a rendered `under_review` swap
    -- and this is the same refusal for the thing a phone camera acts on.

    MUTATION: drop the accepting check and a completed swap offers a QR that
    sends money into a swap nothing will advance.
    """
    pay = payment_options(sol_swap(status=status))

    assert pay["uri"] == ""
    assert pay["qr"] is None
    assert pay["wallets"] == []
    assert pay["show_wallets"] is False


def test_a_non_sol_swap_is_offered_nothing_rather_than_a_solana_request():
    """XRP is tag-attributed too and is deliberately NOT wired in.

    Its deposit request format is an X-address, which
    services/xrp_tag_service.py refuses to allocate against for reasons written
    out there. A view that quietly produced a Solana URI for an XRP swap would be
    worse than one that produces nothing.
    """
    for asset in ("GRC", "BTC", "XRP", ""):
        pay = payment_options(sol_swap(from_asset=asset))
        assert pay["uri"] == "", asset
        assert pay["qr"] is None, asset


def test_a_swap_with_no_tag_is_offered_nothing_instead_of_raising():
    """payment_uri() refuses half an instruction; the view must not turn that into a 500.

    Such a swap already renders deposit.problem on the page, and an exception
    from the payment panel would replace that specific explanation with an error
    page.
    """
    pay = payment_options(sol_swap(deposit_tag=None))

    assert pay["uri"] == ""
    assert pay["qr"] is None


def test_every_field_is_present_even_when_empty():
    """A template reading view.pay.uri for a GRC swap gets "", not an exception.

    Jinja's default undefined raises on an attribute that is absent, so a missing
    key would take out the page for a chain this feature does not cover. Rule
    14's "(none) is a result", applied to a view dict.
    """
    for swap in (sol_swap(), sol_swap(from_asset="GRC"), sol_swap(status="completed")):
        pay = payment_options(swap)
        assert set(pay) == {"uri", "qr", "qr_unavailable", "wallets", "show_wallets"}
