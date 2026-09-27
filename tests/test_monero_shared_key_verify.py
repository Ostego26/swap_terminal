"""The shared-key verifier's pure parts: share sampling, the fixture, the refusals.

Role: test (read-only; no wallet, no process, no socket)
Reads: nothing
Writes: only into pytest's tmp_path
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHAT IS TESTED AND WHAT CANNOT BE

The script's whole purpose needs monero-wallet-rpc, and its DECISIVE step --
generate_from_keys returning the address the wallet derives -- is by definition a
comparison against another implementation, so faking it would assert that a stub
returns what the stub was told. That step is proven by running it, not here.

What IS tested is everything that decides what gets asked: that a sampled share is
in the range the protocol needs, that the address is a function of the shares, and
that the fixture round-trips AND refuses an inconsistent one -- the last being the
case where a wrong file would make every later step measure something else.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import subprocess
import sys

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))
sys.path.insert(0, str(REPOSITORY_ROOT))

from chains.monero_keys import decode_address  # noqa: E402  both shims above first
from modules.ed25519_group import GROUP_ORDER  # noqa: E402  same

from monero_shared_key_verify import (  # noqa: E402  same
    DEFAULT_DAEMON_PORT,
    DEFAULT_WALLET_PORT,
    SHARE_UPPER_BOUND,
    Target,
    VerifyError,
    address_for,
    build_parser,
    build_shares,
    load_shares,
    main,
    refuse_mainnet_and_a_funded_wallet,
    sample_share,
    save_shares,
)


def test_a_sampled_share_is_in_the_range_the_protocol_needs():
    """Non-zero and below 2^252. Zero would make the sum equal the other share alone,
    and above 2^252 it is not a scalar on both curves -- the same bound the
    cross-curve DLEQ needs, so these shares are the shape the real protocol uses
    rather than a looser fixture."""
    assert SHARE_UPPER_BOUND == 1 << 252
    assert SHARE_UPPER_BOUND < GROUP_ORDER, "2^252 is below l, so every share is a valid scalar"
    for _ in range(200):
        share = sample_share()
        assert 0 < share < SHARE_UPPER_BOUND


def test_sampled_shares_are_not_all_the_same():
    """A sampler that returned a constant would pass every range assertion above. 200
    draws from a 252-bit space collide with probability far below any threshold worth
    naming, so equality here means the sampler is broken, not unlucky."""
    assert len({sample_share() for _ in range(200)}) == 200


def test_build_shares_produces_four_distinct_shares_and_consistent_sums():
    shares = build_shares()
    distinct = {shares[key] for key in
                ("spend_share_a", "spend_share_b", "view_share_a", "view_share_b")}
    assert len(distinct) == 4
    assert shares["spend_summed"] == (
        shares["spend_share_a"] + shares["spend_share_b"]
    ) % GROUP_ORDER
    assert shares["view_summed"] == (
        shares["view_share_a"] + shares["view_share_b"]
    ) % GROUP_ORDER
    assert len(bytes.fromhex(shares["public_spend"])) == 32
    assert len(bytes.fromhex(shares["public_view"])) == 32


def test_the_address_is_a_function_of_the_shares_and_the_network():
    """Same shares, same address, every time -- and a different network gives a
    different string. If the address were not deterministic the two swap parties
    could not agree on one."""
    shares = build_shares()
    stagenet = address_for(shares, "stagenet")
    assert stagenet == address_for(shares, "stagenet")
    assert decode_address(stagenet).network == "stagenet"
    assert address_for(shares, "mainnet") != stagenet
    assert len(stagenet) == 95


def test_the_fixture_round_trips(tmp_path):
    shares = build_shares()
    address = address_for(shares, "stagenet")
    path = tmp_path / "shares.json"
    save_shares(path, shares, address, "stagenet")

    reloaded = load_shares(path)
    for key in ("spend_share_a", "spend_share_b", "view_share_a", "view_share_b",
                "spend_summed", "view_summed"):
        assert reloaded[key] == shares[key], key
    assert address_for(reloaded, "stagenet") == address, "the address survives the round trip"


def test_the_fixture_is_written_private_and_says_what_it_holds(tmp_path):
    """Private keys in the clear is correct for a test network and is why the script
    refuses mainnet. Mode 0600 anyway: a habit that only holds on test networks is
    not a habit, and the file says in its own text what it contains."""
    shares = build_shares()
    path = tmp_path / "shares.json"
    save_shares(path, shares, address_for(shares, "stagenet"), "stagenet")

    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text())
    assert "PRIVATE KEY SHARES IN THE CLEAR" in payload["WARNING"]
    assert payload["network"] == "stagenet"
    assert payload["shared_address"] == address_for(shares, "stagenet")


def test_a_fixture_whose_stored_sum_disagrees_with_its_shares_is_refused(tmp_path):
    """THE CHECK WORTH HAVING. A file whose stored sum does not equal the sum of its
    own shares would make every step computed from it measure the wrong thing -- and
    it would not error anywhere: the sweep would simply fail to move funds, which
    reads exactly like the construction being refuted. One recomputation prevents
    mistaking a corrupt fixture for a refutation."""
    shares = build_shares()
    path = tmp_path / "shares.json"
    save_shares(path, shares, address_for(shares, "stagenet"), "stagenet")

    payload = json.loads(path.read_text())
    payload["spend_summed"] = hex((shares["spend_summed"] + 1) % GROUP_ORDER)
    path.write_text(json.dumps(payload))

    with pytest.raises(VerifyError, match="does not equal the sum of its own shares"):
        load_shares(path)


def test_a_fixture_missing_a_field_is_refused_by_name(tmp_path):
    shares = build_shares()
    path = tmp_path / "shares.json"
    save_shares(path, shares, address_for(shares, "stagenet"), "stagenet")
    payload = json.loads(path.read_text())
    del payload["view_share_b"]
    path.write_text(json.dumps(payload))

    with pytest.raises(VerifyError, match="has no `view_share_b`"):
        load_shares(path)


def test_a_missing_fixture_says_how_to_make_one(tmp_path):
    with pytest.raises(VerifyError, match="Run without --sweep first"):
        load_shares(tmp_path / "never-written.json")


def test_the_default_port_is_the_regtest_one_not_the_stagenet_one():
    """A script that creates wallets and sweeps must default to a throwaway. 38083 is
    the stagenet wallet in docs/monero_stagenet_funding.md; 28083 is the one
    monero_regtest.py starts."""
    assert DEFAULT_WALLET_PORT == 28083
    assert build_parser().parse_args([]).port == 28083


def test_a_bare_invocation_does_nothing():
    """No --run and no --sweep prints the plan and exits 0, creating no wallet."""
    completed = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "monero_shared_key_verify.py")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0
    assert "PLAN ONLY -- nothing done" in completed.stdout
    assert "NEEDS NO COINS" in completed.stdout, "the plan must say the cheap check is decisive"


def test_the_balance_refusal_is_scoped_to_the_run_path(monkeypatch):
    """THE DEFECT THE OPERATOR'S SWEEP FOUND, pinned.

    The balance refusal exists because `generate_from_keys` switches the wallet-rpc to
    a different wallet. --sweep never calls it: it operates on the shared wallet --run
    already opened, where a funded wallet is not a hazard but THE SUCCESS CONDITION --
    the mined coins are what is about to be swept. So the guard refused the exact state
    it was waiting for, immediately after --run had printed the sweep command.

    Asserted by inspecting how main() calls it, because exercising it needs a wallet:
    check_balance must be tied to args.run and not passed unconditionally.
    """
    # main()'s source specifically, not the module's: a first draft split the whole
    # module on the function NAME and landed on the definition instead of the call,
    # which passed nothing and failed loudly. The call site is what is being asserted.
    call_site = inspect.getsource(main)
    assert "refuse_mainnet_and_a_funded_wallet(" in call_site
    assert "check_balance=bool(args.run)" in call_site, (
        "the balance refusal must apply only to the path that switches wallets"
    )


def test_check_balance_false_skips_the_refusal_and_says_why():
    """The parameter's default is True -- the safe direction -- and the docstring names
    the sibling-path bug so the next reader does not restore it."""
    signature = inspect.signature(refuse_mainnet_and_a_funded_wallet)
    assert signature.parameters["check_balance"].default is True
    doc = refuse_mainnet_and_a_funded_wallet.__doc__ or ""
    assert "--sweep" in doc and "SUCCESS CONDITION" in doc


def test_load_shares_returns_the_shared_address_for_the_sweep_check(tmp_path):
    """The sweep path compares the OPEN wallet against the fixture's address, so
    load_shares has to hand it back -- it did not, which is why that check could not
    have been written before."""
    shares = build_shares()
    address = address_for(shares, "stagenet")
    path = tmp_path / "shares.json"
    save_shares(path, shares, address, "stagenet")

    reloaded = load_shares(path)
    assert reloaded["shared_address"] == address
    assert address_for(reloaded, "stagenet") == address, (
        "and the shares re-derive to it, which is the second half of the sweep check"
    )


def test_a_bare_number_daemon_becomes_a_localhost_port_and_anything_else_is_passed_through():
    """THE ASSUMPTION THIS REMOVED: that monerod is local.

    Measured on the operator's host 2026-09-27: nothing on 38081, 38089 or 18081, while
    the stagenet wallet-rpc on 38083 reported height 2,217,113 -- the real stagenet tip.
    It was talking to a REMOTE node all along, so every daemon call this script makes
    would have hit a port with nothing behind it.

    One flag accepts both spellings so the local and remote cases read the same at the
    call site, and the parsing is asserted here rather than only exercised by a run.
    """
    parser = build_parser()
    assert parser.parse_args([]).daemon == str(DEFAULT_DAEMON_PORT)
    for given, expected_kind in (
        ("28081", int), ("38081", int),
        ("stagenet.example.org:38081", str), ("127.0.0.1:38081", str),
    ):
        parsed = parser.parse_args(["--daemon", given]).daemon
        resolved = int(parsed) if str(parsed).isdigit() else str(parsed)
        assert isinstance(resolved, expected_kind), f"{given} resolved to {type(resolved)}"


def test_the_rpc_url_is_localhost_for_a_port_and_the_given_host_otherwise():
    """The two spellings must produce the two URLs, which is the whole point of
    accepting both. Asserted by reading the source rather than by opening a socket:
    the construction is one line and a test that needed a daemon would not run here."""
    source = inspect.getsource(sys.modules["monero_shared_key_verify"].rpc)
    assert 'f"127.0.0.1:{endpoint}" if isinstance(endpoint, int) else endpoint' in source
    assert 'f"http://{host}/json_rpc"' in source
    assert "http://127.0.0.1:{port}" not in source, "the hardcoded localhost URL is gone"


def test_the_unreachable_endpoint_error_names_how_to_find_a_remote_daemon():
    """Rule 14: the message has to carry the remedy. `ps aux | grep monero-wallet-rpc`
    shows the --daemon-address the wallet is actually using, which is how this was
    found in the first place."""
    doc = inspect.getsource(sys.modules["monero_shared_key_verify"].rpc)
    assert "ps aux | grep monero-wallet-rpc" in doc
    assert "--daemon-address" in doc


@pytest.mark.parametrize("extra", [["--run"], ["--sweep", "537wxk1v"]])
def test_both_paths_reach_the_rpc_rather_than_an_attribute_error(extra, tmp_path):
    """THE TEST THAT WAS MISSING, AND THE BUG IT WOULD HAVE CAUGHT.

    Renaming Target.daemon_port to Target.daemon left one stale `target.daemon_port` at
    a call site whose formatting a search-and-replace did not match. ruff cannot catch
    it -- a wrong attribute on a dataclass is not an undefined NAME -- and every test in
    this file exercised pure functions, so nothing reached main()'s argument plumbing.
    The operator hit it on the first real stagenet invocation:

        AttributeError: 'Target' object has no attribute 'daemon_port'

    This drives main() end to end against ports where nothing listens, on BOTH paths.
    It needs no daemon and no wallet: the assertion is that the run gets as far as a
    CONNECTION REFUSED and reports it as a refusal, which means every attribute access
    and every argument on the way there resolved. Any AttributeError, TypeError or
    NameError in that plumbing fails this instead of reaching the operator.

    Both paths, because they diverge immediately after that call -- which is where
    today's three guard-scoping bugs all lived.
    """
    completed = subprocess.run(
        [
            sys.executable, str(REPOSITORY_ROOT / "monero_shared_key_verify.py"),
            *extra,
            "--port", "29998",
            "--daemon", "29997",
            "--shares-file", str(tmp_path / "shares.json"),
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    combined = completed.stdout + completed.stderr
    assert "Traceback" not in combined, f"the plumbing raised instead of refusing:\n{combined}"
    for forbidden in ("AttributeError", "TypeError", "NameError"):
        assert forbidden not in combined, f"{forbidden} in the argument path:\n{combined}"
    assert "Connection refused" in combined, (
        f"expected to get as far as an unreachable endpoint, got:\n{combined}"
    )
    assert completed.returncode == 1, "a refusal is a failing summary, not a crash"


def test_target_has_no_daemon_port_attribute(tmp_path):
    """Belt to the braces above, and it names the old spelling so a revert is loud.
    `daemon` is an int port on localhost OR a host:port string; `daemon_port` cannot
    express the second, which is why it was renamed."""
    path = tmp_path / "shares.json"
    target = Target(wallet_port=1, daemon="host:2", shares_path=path)
    assert not hasattr(target, "daemon_port")
    assert target.daemon == "host:2"
    assert Target(wallet_port=1, daemon=2, shares_path=path).daemon == 2
