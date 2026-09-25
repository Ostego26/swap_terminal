"""fund_testnets.py's decisions, and its refusal to write a secret into a repo.

Role: test (pure functions and a filesystem guard; no daemon, no network)
Reads: fund_testnets.py
Writes: a temp directory, in the git-guard test only
Can move funds: no. The script it tests mints only test coins; these tests
      mint nothing and open no socket.
Mainnet-safe: yes
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The path insert above has to run first: fund_testnets.py is at the project
# root, which conftest.py does not put on sys.path.
from fund_testnets import (
    COINBASE_MATURITY_BLOCKS,
    SOLANA_DEVNET,
    XRP_FAUCET,
    secret_destination,
    selected_chains,
)


class Args:
    """Stands in for argparse's namespace."""

    def __init__(self, **kwargs):
        for name in ("btc", "ltc", "xrp", "sol", "xmr", "all"):
            setattr(self, name, kwargs.get(name, False))
        self.blocks = kwargs.get("blocks", COINBASE_MATURITY_BLOCKS)


def test_nothing_selected_selects_nothing():
    """So main() can refuse with the help text rather than doing something."""
    assert selected_chains(Args()) == []


def test_one_flag_selects_exactly_that_chain():
    assert [asset for asset, _, _ in selected_chains(Args(btc=True))] == ["BTC"]
    assert [asset for asset, _, _ in selected_chains(Args(xrp=True))] == ["XRP"]


def test_all_selects_every_chain_and_gridcoin_is_not_among_them():
    """GRC is excluded at the operator's request -- they already hold testnet GRC.

    Pinned so that "helpfully" adding it later is deliberate. Mining GRC here
    would also be a different mechanism again, since Gridcoin has no regtest in
    the form this script uses.
    """
    assets = [asset for asset, _, _ in selected_chains(Args(all=True))]
    assert assets == ["BTC", "LTC", "XRP", "SOL", "XMR"]
    assert "GRC" not in assets
    assert "ETH" not in assets


def test_monero_has_no_runner_so_it_cannot_pretend():
    """XMR's entry is None on purpose: the script EXPLAINS rather than attempts.

    A stagenet sync was measured at 33-54 hours, so a runner here would either
    block for a day or produce a wallet that cannot see its own deposit.
    """
    xmr = next(entry for entry in selected_chains(Args(all=True)) if entry[0] == "XMR")
    assert xmr[2] is None


def test_every_other_chain_has_a_runner():
    for asset, _, run in selected_chains(Args(all=True)):
        if asset != "XMR":
            assert callable(run), asset


def test_the_default_block_count_clears_coinbase_maturity():
    """Fewer than 101 leaves the reward immature and the balance reading zero.

    Which looks like a broken daemon rather than immature coins -- so the
    default has to be the number that actually yields spendable coins.
    """
    assert COINBASE_MATURITY_BLOCKS == 101


def test_the_hosts_are_pinned_to_test_networks():
    """No flag reaches mainnet; changing that means editing the source.

    Asserted rather than trusted, because a script that mints coins should not
    be one typo away from a mainnet endpoint.
    """
    assert "altnet.rippletest.net" in XRP_FAUCET
    assert "devnet" in SOLANA_DEVNET
    assert "mainnet" not in XRP_FAUCET and "mainnet" not in SOLANA_DEVNET
    assert "s1.ripple.com" not in XRP_FAUCET


def test_a_secret_is_never_written_inside_a_git_repository(tmp_path, monkeypatch):
    """THE GUARD THAT MATTERS, and it has a date on it.

    wgrc.json was a Solana keypair written inside this working tree, committed,
    and pushed -- which cost a key rotation and a history rewrite on
    2026-09-25. A secret path inside a repo is one `git add -A` from being
    published, so this refuses rather than trusting the .gitignore.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".config" / "swap_terminal" / "keys").mkdir(parents=True)
    (fake_home / ".git").mkdir()          # a repo ABOVE the key directory
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    with pytest.raises(Exception, match="inside a git repository"):
        secret_destination("xrp")


def test_a_secret_destination_outside_any_repo_is_accepted_and_locked_down(tmp_path, monkeypatch):
    fake_home = tmp_path / "clean"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    destination = secret_destination("xrp")
    assert destination.parent.is_dir()
    assert oct(destination.parent.stat().st_mode)[-3:] == "700"
    assert "xrp-testnet-" in destination.name
    assert destination.suffix == ".json"


def test_two_secret_destinations_in_the_same_run_never_collide(tmp_path, monkeypatch):
    """A second call must not hand back a path the first one will write to.

    The timestamp is second-resolution and the write uses write_text(), so
    without a uniqueness step two calls inside one second would silently
    overwrite the first key with the second. An earlier version of this test
    xfailed on exactly that -- documenting a key-loss bug instead of fixing a
    two-line problem, which is precisely what rule 19 forbids.
    """
    fake_home = tmp_path / "clean2"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    first = secret_destination("xrp")
    first.write_text("{}")
    second = secret_destination("xrp")
    assert first != second, "a second call returned a path that would overwrite the first key"
    second.write_text("{}")
    third = secret_destination("xrp")
    assert third not in (first, second)


def test_an_existing_secret_file_is_never_overwritten(tmp_path, monkeypatch):
    """Whatever is already there could be a funded account, so it is left alone."""
    fake_home = tmp_path / "clean3"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    taken = secret_destination("xrp")
    taken.write_text('{"account": "do not lose me"}')
    assert secret_destination("xrp") != taken
    assert taken.read_text() == '{"account": "do not lose me"}'
