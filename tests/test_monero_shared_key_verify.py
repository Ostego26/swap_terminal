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
import urllib.error

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))
sys.path.insert(0, str(REPOSITORY_ROOT))

from chains.monero_keys import decode_address  # noqa: E402  both shims above first
from modules.ed25519_group import GROUP_ORDER  # noqa: E402  same
from step_console import Console  # noqa: E402  same

import monero_shared_key_verify as harness  # noqa: E402  same
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
    open_wallet_address,
    preflight_run,
    preflight_sweep,
    sample_share,
    save_shares,
    shared_wallet_name,
    sweep_command,
    sync_state,
    wait_for_wallet,
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


def test_the_two_paths_have_their_own_preflight_and_share_no_flags():
    """THE STRUCTURAL FIX, replacing two tests that pinned the flag they removed.

    One shared `refuse_mainnet_and_a_funded_wallet(..., allow_open_wallet, check_balance)`
    produced FOUR bugs in one day, all the same shape: a guard written for the state one
    entry path leaves behind, applied to a sibling that does not produce it. Each was
    patched by adding a parameter, and bugs three and four exist BECAUSE of the
    patching -- two booleans on one function is a function doing two jobs and trusted to
    remember which.

    So it is two functions now, sharing exactly the one check that is genuinely common.
    This asserts the shape rather than any single flag, because the shape is what
    prevents instance five.
    """
    module = sys.modules["monero_shared_key_verify"]
    assert not hasattr(module, "refuse_mainnet_and_a_funded_wallet"), (
        "the shared preflight is gone; adding a path adds a preflight, not a boolean"
    )

    # Neither preflight takes a flag that says "behave like the other one".
    for function in (preflight_run, preflight_sweep):
        names = set(inspect.signature(function).parameters)
        assert "check_balance" not in names, f"{function.__name__} regrew a mode flag"
        assert {"console", "port", "daemon"} <= names, f"{function.__name__} lost an input"
    assert "allow_open_wallet" in inspect.signature(preflight_run).parameters, (
        "consent to closing a funded wallet belongs on the path that closes one"
    )
    assert "allow_open_wallet" not in inspect.signature(preflight_sweep).parameters, (
        "--sweep closes no wallet, so it has nothing to consent to"
    )

    # And main() routes to them per path rather than passing a mode.
    call_site = inspect.getsource(main)
    assert "preflight_run(" in call_site and "preflight_sweep(" in call_site


def test_the_two_preflights_have_opposite_requirements_on_an_open_wallet():
    """The inversion is the point, and it is what bug four was: --run CREATES the wallet
    so none being open is the good case; --sweep SPENDS from one so a wallet must be
    open and its balance is the success condition. Asserted from the docstrings, which
    is where a reader will look before changing either."""
    run_doc = preflight_run.__doc__ or ""
    sweep_doc = preflight_sweep.__doc__ or ""
    assert "NO WALLET OPEN IS THE GOOD CASE" in run_doc
    assert "MUST be open" in sweep_doc
    assert "success condition" in sweep_doc


def test_open_wallet_address_returns_none_for_no_wallet_and_reraises_anything_else():
    """BUG FOUR, pinned at its source. A fresh --wallet-dir process answers get_address
    with code -13 "No wallet file" -- a STATE, not a fault, and the normal state for
    --run. Any other error is a real problem and must not be swallowed into None, which
    would make an unreachable wallet indistinguishable from an empty one."""
    source = inspect.getsource(open_wallet_address)
    assert '"No wallet file" in str(error)' in source
    assert "raise" in source, "a different error must propagate rather than become None"
    doc = open_wallet_address.__doc__ or ""
    assert "-13" in doc and "state rather than a fault" in doc


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


class _StubDaemonAndWallet:
    """A stand-in for rpc(), so the two preflights can be driven with no daemon at all.

    WHY THIS EXISTS: the first draft of the tests above asserted the DOCSTRINGS of
    preflight_run and preflight_sweep -- "NO WALLET OPEN IS THE GOOD CASE" appears in
    the text -- and a mutation that restored bug four (raising when no wallet is open)
    PASSED, because the docstring still said the right thing while the code no longer
    did. A test that reads prose cannot catch a change in behavior. This one calls the
    functions.

    It is deliberately a dumb dispatcher rather than a mock framework: what is under
    test is which RPCs each preflight makes and how it reacts, so the stub records the
    calls and answers from a dict, and a method nobody configured raises instead of
    returning a plausible empty value.
    """

    def __init__(self, nettype="stagenet", wallet_address=None, balance=0):
        self.nettype = nettype
        self.wallet_address = wallet_address
        self.balance = balance
        self.calls: list[str] = []

    def __call__(self, endpoint, method, params=None, timeout=120):
        self.calls.append(method)
        if method == "get_version":
            return {"version": 65562}
        if method == "get_info":
            return {"nettype": self.nettype, "height": 2217113}
        if method == "get_address":
            if self.wallet_address is None:
                raise VerifyError("get_address on 127.0.0.1:1: {'code': -13, 'message': 'No wallet file'}")
            return {"address": self.wallet_address}
        if method == "get_balance":
            return {"balance": self.balance, "unlocked_balance": self.balance}
        raise AssertionError(f"the stub was not configured for {method!r}")


STAGENET_PRIMARY = (
    "537wxk1vzCDembafqWxfTgNcZGoK6rAsbP1JHKiQkjYLLzNDtgMTUKACBguFzx2XnFf1FQVqogcjd9LXTQ52jGiVBV52C1V"
)


def _with_stub(monkeypatch, stub):
    monkeypatch.setattr(sys.modules["monero_shared_key_verify"], "rpc", stub)
    return stub


def test_preflight_run_ACCEPTS_a_port_with_no_wallet_open(monkeypatch):
    """BUG FOUR, pinned behaviorally this time.

    A fresh `--wallet-dir` wallet-rpc has no wallet open and answers get_address with
    code -13. --run's whole job is to CREATE the shared wallet, so that is the good case
    and must not refuse -- it did, on the operator's first real stagenet invocation,
    after every other check had passed.
    """
    stub = _with_stub(monkeypatch, _StubDaemonAndWallet(wallet_address=None))
    console = Console(total_steps=1)
    assert preflight_run(console, 38084, "node.example.org:38089", False) == "stagenet"
    assert all(ok for _, ok in console.results), "no check may fail on the good case"
    assert "get_balance" not in stub.calls, (
        "with no wallet open there is no balance to read, and reading one would be the "
        "same conflation bug in a new place"
    )


def test_preflight_run_refuses_a_funded_open_wallet_unless_consented(monkeypatch):
    """The reason the guard exists at all: generate_from_keys switches wallets."""
    stub = _StubDaemonAndWallet(wallet_address=STAGENET_PRIMARY, balance=10000000000)
    _with_stub(monkeypatch, stub)
    with pytest.raises(VerifyError, match="holds 10000000000 atomic units"):
        preflight_run(Console(total_steps=1), 38083, 38081, False)

    # And with consent it proceeds, saying what it costs.
    _with_stub(monkeypatch, _StubDaemonAndWallet(wallet_address=STAGENET_PRIMARY, balance=10000000000))
    assert preflight_run(Console(total_steps=1), 38083, 38081, True) == "stagenet"


def test_preflight_run_accepts_an_open_wallet_that_is_empty(monkeypatch):
    """Neither refusal applies: a wallet is open but holds nothing, which is what
    monero_regtest.py leaves before any mining."""
    _with_stub(monkeypatch, _StubDaemonAndWallet(wallet_address=STAGENET_PRIMARY, balance=0))
    assert preflight_run(Console(total_steps=1), 28083, 28081, False) == "stagenet"


def test_preflight_sweep_REFUSES_a_port_with_no_wallet_open(monkeypatch):
    """The exact inverse, which is why they are two functions: --sweep spends from the
    open wallet, so none being open is nothing to sweep."""
    _with_stub(monkeypatch, _StubDaemonAndWallet(wallet_address=None))
    with pytest.raises(VerifyError, match="no wallet is open"):
        preflight_sweep(Console(total_steps=1), 38084, 38081)


def test_preflight_sweep_accepts_a_funded_open_wallet(monkeypatch):
    """BUG THREE, pinned behaviorally for the same reason as bug four: a funded wallet
    here is the SUCCESS condition -- the coins about to be swept -- and the old shared
    guard refused it."""
    stub = _StubDaemonAndWallet(wallet_address=STAGENET_PRIMARY, balance=738741466321372)
    _with_stub(monkeypatch, stub)
    console = Console(total_steps=1)
    assert preflight_sweep(console, 38084, 38081) == "stagenet"
    assert all(ok for _, ok in console.results)


@pytest.mark.parametrize("path", ["run", "sweep"])
def test_both_preflights_refuse_mainnet(monkeypatch, path):
    """The one check they genuinely share, asserted on both so a split cannot drop it
    from one. The shares are written to a file in the clear; on mainnet that is a
    key-disclosure bug rather than a fixture."""
    _with_stub(monkeypatch, _StubDaemonAndWallet(nettype="mainnet", wallet_address=STAGENET_PRIMARY))
    with pytest.raises(VerifyError, match="MAINNET"):
        if path == "run":
            preflight_run(Console(total_steps=1), 18083, 18081, True)
        else:
            preflight_sweep(Console(total_steps=1), 18083, 18081)


def test_rpc_catches_a_read_timeout_and_not_only_a_connect_failure():
    """THE RAW TRACEBACK THE OPERATOR GOT, pinned at its cause.

    urllib wraps a failure to CONNECT in URLError, but a READ that times out raises
    TimeoutError straight out of socket.recv_into -- and TimeoutError is NOT a URLError.
    So a wallet-rpc that accepted the connection and then went quiet, which is exactly
    what a freshly created wallet scanning a remote chain does, produced fourteen frames
    of traceback instead of a sentence. It never happened on regtest, where the wallet
    answers instantly; stagenet is what surfaced it.

    Both are OSError subclasses, so the assertion is that the one clause covering the
    whole class is what is caught -- not the two members I happened to think of.
    """
    source = inspect.getsource(sys.modules["monero_shared_key_verify"].rpc)
    assert "except OSError as error:" in source
    assert "except urllib.error.URLError as error:" not in source, (
        "URLError alone misses a read timeout, which is the failure that reached the operator"
    )
    # The relationship the fix depends on, asserted rather than assumed.
    assert issubclass(TimeoutError, OSError)
    assert issubclass(urllib.error.URLError, OSError)


def test_wait_for_wallet_retries_a_busy_wallet_and_then_gives_up_with_a_reason(monkeypatch):
    """A new wallet is BUSY, not absent, and the difference decides whether to wait.

    monero-wallet-rpc serves RPC on one thread; a wallet created by generate_from_keys
    against a remote node refreshes before it answers anything. Treating that as
    unreachable is wrong twice: the wallet is there, and waiting is the right response.
    """
    attempts = {"n": 0}

    def busy_then_ready(endpoint, method, params=None, timeout=120):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise VerifyError("get_version on 127.0.0.1:38084: timed out")
        return {"version": 65562}

    monkeypatch.setattr(sys.modules["monero_shared_key_verify"], "rpc", busy_then_ready)
    monkeypatch.setattr(sys.modules["monero_shared_key_verify"].time, "sleep", lambda _s: None)
    console = Console(total_steps=1)
    assert wait_for_wallet(console, 38084, seconds=300)["version"] == 65562
    assert attempts["n"] == 3, "it retried rather than failing on the first timeout"

    def always_busy(endpoint, method, params=None, timeout=120):
        raise VerifyError("timed out")

    monkeypatch.setattr(sys.modules["monero_shared_key_verify"], "rpc", always_busy)
    monkeypatch.setattr(
        sys.modules["monero_shared_key_verify"].time, "monotonic",
        lambda _c=[0]: (_c.__setitem__(0, _c[0] + 100), _c[0])[1],
    )
    with pytest.raises(VerifyError, match="did not answer get_version within"):
        wait_for_wallet(Console(total_steps=1), 38084, seconds=10)


def test_a_refused_connection_is_not_retried_as_a_busy_wallet(monkeypatch):
    """THE REGRESSION MY OWN FIX INTRODUCED, pinned.

    wait_for_wallet() exists because a busy wallet times out and should be waited for.
    Written first, it retried EVERY failure -- including "Connection refused", which
    means nothing is listening and no amount of waiting changes that. Cost: 300 seconds
    per invocation against a wrong port, and it took the full suite from 37 seconds to
    275 with two failures.

    Same conflation shape as the four before it: two different failures treated as one.
    A timeout means something accepted the connection and has not answered; refused
    means nothing is there.
    """
    calls = {"n": 0}

    def refused(endpoint, method, params=None, timeout=120):
        calls["n"] += 1
        raise VerifyError("get_version on 127.0.0.1:29998: <urlopen error [Errno 111] Connection refused>")

    monkeypatch.setattr(sys.modules["monero_shared_key_verify"], "rpc", refused)
    monkeypatch.setattr(sys.modules["monero_shared_key_verify"].time, "sleep", lambda _s: None)
    with pytest.raises(VerifyError, match="nothing is listening"):
        wait_for_wallet(Console(total_steps=1), 29998, seconds=900)
    assert calls["n"] == 1, f"refused must not be retried; it was tried {calls['n']} times"



# ---------------------------------------------------------------------------
# THE WALLET FILENAME. A fixed one meant the script worked exactly once.
# ---------------------------------------------------------------------------

_ADDRESS_A = "42tYMtaj4PRQQg1UPd3xr2KbEkqgwCjNcJBYcL112kDVRy3PHYgWvi6TfPbkmpAs3UXjYkNcEXwRb3JwzQXKuzXbBsPCKbZ"
_ADDRESS_B = "47L9GEy71w83v2kFYAjoLsCj5YrBGGg2rBTJh3ZGJJidLMLSBqcQXLsXYMAYBwvwRMSNYYVAJ97aW3Emp5NLqBy2KcpFQAy"


def test_two_runs_do_not_collide_on_the_wallet_file():
    """MEASURED ON THE OPERATOR'S HOST, 2026-09-28, and it is the whole reason this exists.

    The filename was the fixed string "shared-2of2". Every --run samples FRESH shares and so
    computes a FRESH address, then asked monero-wallet-rpc to write it to the file the previous
    run had already made:

        FAIL  run: generate_from_keys: {'code': -1, 'message': 'Wallet already exists.'}

    So the script worked exactly once per wallet directory. These are the two real addresses
    from that host -- the one on disk and the one the failing run computed.
    """
    assert shared_wallet_name(_ADDRESS_A) != shared_wallet_name(_ADDRESS_B)


def test_the_same_shares_DO_collide_and_that_is_the_useful_case():
    """Deterministic on the address, so "Wallet already exists" stops being spurious and starts
    meaning "you already built this exact wallet" -- which is a thing worth being told."""
    assert shared_wallet_name(_ADDRESS_A) == shared_wallet_name(_ADDRESS_A)


def test_the_name_carries_the_address_so_ls_matches_what_the_script_printed():
    """A prefix and not a hash. A reader in the wallet directory can match the file against the
    address on screen; a hash would make that a lookup. Nothing reads the name back -- step 3
    asserts the wallet's identity from the address the daemon derives."""
    name = shared_wallet_name(_ADDRESS_A)
    assert name.startswith("shared-2of2-")
    assert _ADDRESS_A.startswith(name.removeprefix("shared-2of2-"))


def test_an_empty_address_is_refused_rather_than_naming_a_file_after_nothing():
    """It would produce "shared-2of2-", a name that collides with every other empty-address run,
    and it would mean the arithmetic above produced nothing -- which is worth a sentence."""
    with pytest.raises(VerifyError):
        shared_wallet_name("")


def test_THE_PRINTED_SWEEP_COMMAND_CARRIES_EVERY_ARGUMENT_IT_NEEDS():
    """The command printed at the end of a stagenet --run could not work, 2026-09-29.

    It read `--sweep <address> --port 38084` and stopped there, and both omissions are fatal in
    different ways:

      --daemon       falls back to DEFAULT_DAEMON_PORT, monero_regtest.py's LOCAL regtest
                     daemon. On a stagenet run the daemon is REMOTE, nothing is on that port,
                     and step 1 fails with the same "nothing is listening" the operator had
                     already hit twice that evening.
      --shares-file  falls back to ~/xmr-regtest/shared-shares.json, the REGTEST path, while a
                     stagenet run passes its own. THAT FILE USUALLY EXISTS -- the regtest runs
                     of 2026-09-27 and 2026-09-28 left one -- so the sweep does not fail
                     cleanly. It loads a DIFFERENT share set, recomputes a DIFFERENT address,
                     and reports a mismatch against a wallet whose keys were never wrong. A
                     wrong answer that looks like a finding is worse than an error.

    The defaults are what make the omission dangerous rather than merely wrong, so they are
    asserted here: if either default ever becomes the stagenet value, this test should be read
    again rather than trusted.
    """
    line = sweep_command("5B5yTESTADDRESS", 38084, "node.monerodevs.org:38089",
                         pathlib.Path("/home/op/xmr-stagenet-shared/shared-shares.json"))
    assert "--sweep 5B5yTESTADDRESS" in line
    assert "--port 38084" in line
    assert "--daemon node.monerodevs.org:38089" in line
    assert "--shares-file /home/op/xmr-stagenet-shared/shared-shares.json" in line
    # AND --wait, because the parser's default of 300s expires inside Monero's 10-block lock.
    # Measured 2026-09-29: the faucet paid at block 2218209 with the tip at 2218215 -- six
    # blocks into a ten-block lock -- so the printed command would have timed out four blocks
    # short and reported "nothing UNLOCKED" about coins that were always going to arrive.
    assert f"--wait {harness.SWEEP_WAIT_FOR_A_FRESH_DEPOSIT}" in line
    assert harness.SWEEP_WAIT_FOR_A_FRESH_DEPOSIT >= 1200, (
        "it has to clear a 10-block lock at Monero's 120s target, with margin"
    )

    assert DEFAULT_DAEMON_PORT == 28081, (
        "the regtest daemon port -- which is why omitting --daemon sent a stagenet sweep at a "
        "local port with nothing on it"
    )
    assert "xmr-regtest" in str(_shares_file_default()), (
        "the regtest shares path -- which is why omitting --shares-file could load the WRONG "
        "share set instead of failing"
    )


def _shares_file_default():
    """The parser's own default for --shares-file, READ rather than restated here.

    A copy of the path in this test would be the very thing the test exists to catch: two
    statements of one default, agreeing today (rule 8).
    """
    for action in build_parser()._actions:
        if action.dest == "shares_file":
            return action.default
    raise AssertionError("--shares-file is gone; this test and sweep_command() both need reading")


def test_BOTH_BRANCHES_PRINT_THE_SAME_COMMAND_FROM_THE_SAME_FUNCTION():
    """The string was written twice -- once in the --mine branch, once in the funding branch.

    Rule 8's two copies of one rule, in a line of output: they agreed the day they were written
    and the fix would otherwise have had to be applied to both, which is how one of them stays
    broken.
    """
    source = pathlib.Path(
        inspect.getfile(inspect.getmodule(sweep_command))).read_text(encoding="utf-8")
    body = source[source.index("def run_phase"):]
    assert body.count("sweep_command(") == 2, "both branches call it"
    assert "--sweep {address} " not in body, "and neither still builds its own"


def test_SYNCED_AND_EMPTY_READS_DIFFERENTLY_FROM_STILL_SCANNING(monkeypatch):
    """The two states a bare `balance=0` cannot tell apart, measured 2026-09-29.

    The wait loop printed 28 passes of `refresh 24: balance=0 unlocked=0 (119.6s)` with every
    `Refresh done` beside it saying `blocks received: 0`, and the operator pressed Ctrl-C at
    140s. That output is identical in two situations that need OPPOSITE actions:

      SYNCED, NOTHING ARRIVED   the wallet is at the tip, so it WOULD see a payment. Waiting is
                                right only while the faucet has not paid.
      NOT SYNCING               the wallet is behind and not advancing. Waiting cannot help and
                                the balance means nothing -- the node is not answering, or the
                                wallet is not asking.

    Rule 14 says an operator who cannot tell working from hung presses Ctrl-C, and that is
    precisely what happened, so this is not a cosmetic complaint about a log line.
    """
    def fake(target, method, params=None, timeout=None):
        if method == "get_height":
            return {"height": heights["wallet"]}
        if method == "get_info":
            return {"height": heights["tip"]}
        raise AssertionError(method)

    monkeypatch.setattr(harness, "rpc", fake)

    heights = {"wallet": 2217975, "tip": 2217975}
    wallet, tip, meaning = sync_state(38084, "node:38089")
    assert (wallet, tip) == (2217975, 2217975)
    assert "synced" in meaning and "WOULD be visible" in meaning

    # ONE BLOCK BEHIND IS STILL SYNCED. The wallet is created at tip-1 by design, and a tip that
    # advances between the two calls in this function would otherwise read as "scanning".
    heights = {"wallet": 2217975, "tip": 2217976}
    assert "synced" in sync_state(38084, "node:38089")[2]

    heights = {"wallet": 2217000, "tip": 2217975}
    wallet, tip, meaning = sync_state(38084, "node:38089")
    assert "BEHIND BY 975" in meaning and "not yet meaningful" in meaning
    assert "synced" not in meaning, "the two must not share a sentence -- that was the defect"


def test_AN_UNREADABLE_DAEMON_TIP_IS_A_RESULT_NOT_A_TRACEBACK(monkeypatch):
    """The wallet may still answer from its own cache, so dying here helps nobody.

    "The tip could not be read" is a statement an operator can act on -- it points at the remote
    node -- where a traceback out of a wait loop points at nothing (rule 14).
    """
    def fake(target, method, params=None, timeout=None):
        if method == "get_height":
            return {"height": 2217975}
        raise harness.VerifyError("get_info on node:38089: connection refused")

    monkeypatch.setattr(harness, "rpc", fake)
    wallet, tip, meaning = sync_state(38084, "node:38089")
    assert wallet == 2217975 and tip == 0
    assert "UNREADABLE" in meaning and "did not answer" in meaning


def test_CTRL_C_IN_THE_WAIT_LOOP_SAYS_WHAT_IT_WAS_WAITING_FOR(monkeypatch):
    """Fourteen frames of traceback ending in `time.sleep(5.0)`, 2026-09-29.

    It told the operator nothing about what was being waited for, whether anything had been
    risked, or how to resume. All three are answerable, and the loop only READS -- which is the
    first thing worth saying to someone who just interrupted a money-adjacent script.
    """
    def fake(target, method, params=None, timeout=None):
        if method == "get_height":
            return {"height": 2217975}
        if method == "get_info":
            return {"height": 2217975}
        if method == "get_balance":
            return {"balance": 0, "unlocked_balance": 0}
        if method == "get_transfers":
            return {}
        if method == "refresh":
            raise KeyboardInterrupt
        raise AssertionError(method)

    monkeypatch.setattr(harness, "rpc", fake)
    monkeypatch.setattr(harness.time, "sleep", lambda _s: None)
    console = Console()
    with pytest.raises(harness.VerifyError) as caught:
        harness.report_balance(console, 38084, "node:38089", 60)
    said = str(caught.value)
    assert "interrupted after" in said
    assert "only READS" in said, "the first thing to say to someone who just hit Ctrl-C"
    assert "wallet_height=2217975" in said and "daemon_tip=2217975" in said
    assert "resumes from here" in said, "and how to pick it back up"


def test_THE_REFUSAL_PRINTS_THE_ADDRESS_THE_COINS_ARE_MISSING_FROM(monkeypatch):
    """"the coins were never sent to the shared address" -- without printing it, 2026-09-29.

    The stagenet run waited 300s, established exactly the right thing (synced, node live, pool
    empty, so nothing was ever sent), and refused with a sentence naming "the shared address"
    while never showing it. That string is the one thing the operator has to paste into a faucet,
    and it was in the process's own hands the whole time.

    Rule 14's "echo the parameters that decide the answer": the answer is decided by whether
    anything was sent THERE, so the there belongs on screen -- both while waiting, so a faucet
    can be opened without scrolling, and in the refusal, so the pasted output is self-describing
    a day later.
    """
    address = "5B5yTESTADDRESSxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    def fake(target, method, params=None, timeout=None):
        return {"height": 2217986, "balance": 0, "unlocked_balance": 0}

    monkeypatch.setattr(harness, "rpc", fake)
    monkeypatch.setattr(harness.time, "sleep", lambda _s: None)
    console = Console()
    with pytest.raises(harness.VerifyError) as caught:
        harness.report_balance(console, 38084, "node:38089", 0, address)
    said = str(caught.value)
    assert address in said, "the address, in the refusal that says it was never paid"
    assert "cypherfaucet.com/xmr-stagenet" in said, "and how to pay it on a valueless network"
    assert "only reads" in said, "and that re-running needs nothing undone first"


def test_THE_WAIT_ANNOUNCES_WHICH_ADDRESS_IT_IS_WATCHING(monkeypatch):
    """Announce before, not only after (rule 14). A 300s wait should say what it is waiting on
    at the START, because that is when a faucet tab would be useful -- not 300 seconds later."""
    address = "5B5yANOTHERTESTADDRESS"
    said: list[str] = []

    def fake(target, method, params=None, timeout=None):
        return {"height": 1, "balance": 0, "unlocked_balance": 0}

    monkeypatch.setattr(harness, "rpc", fake)
    monkeypatch.setattr(harness.time, "sleep", lambda _s: None)

    class _Recorder(Console):
        def say(self, line=""):
            said.append(line)

    with pytest.raises(harness.VerifyError):
        harness.report_balance(_Recorder(), 38084, "node:38089", 0, address)
    assert any(address in line and "waiting on THIS address" in line for line in said)


def test_THE_ADDRESS_IS_OPTIONAL_SO_NO_CALLER_IS_BROKEN_BY_IT(monkeypatch):
    """--mine on regtest funds the address itself and has no faucet to point at.

    The parameter defaults to empty and the faucet block is omitted entirely rather than printed
    with a blank in it -- a sentence telling a regtest operator to visit a stagenet faucet would
    be worse than saying nothing (rule 14: state what the number means, and do not state what it
    does not).
    """
    def fake(target, method, params=None, timeout=None):
        return {"height": 1, "balance": 0, "unlocked_balance": 0}

    monkeypatch.setattr(harness, "rpc", fake)
    monkeypatch.setattr(harness.time, "sleep", lambda _s: None)
    with pytest.raises(harness.VerifyError) as caught:
        harness.report_balance(Console(), 38084, "node:38089", 0)
    assert "cypherfaucet" not in str(caught.value)
    assert "SEND ANY AMOUNT TO" not in str(caught.value)


def test_A_HANDOFF_FILE_HAD_NO_PATH_INTO_A_WALLET():
    """The gap found on the operator's host 2026-09-29, with coins already at the address.

    regtest/adaptor_steps.py writes a handoff fixture carrying the Monero share RECOVERED from a
    Gridcoin scriptSig. Nothing could open it: `--run` creates a wallet from FRESHLY SAMPLED
    shares, which is the wrong keys entirely, and `--sweep` requires the wallet to be open
    already and refuses otherwise. So the file that closes gap (e) was unusable by the only
    script that reads its format -- discovered with 0.01 sXMR already sitting at the address.
    """
    parser = build_parser()
    assert parser.parse_args(["--open"]).open_shares is True
    source = pathlib.Path(harness.__file__).read_text(encoding="utf-8")
    assert "def open_phase(" in source
    assert "if args.open_shares:" in source, "and main() dispatches to it"
    # IT MUST NOT SAMPLE. A wallet built from new shares would sweep beautifully and prove
    # nothing, because the scalar would never have touched a chain.
    body = source[source.index("def open_phase("):source.index("def sweep_command(")]
    assert "build_shares" not in body and "sample_share" not in body


def test_THE_RESTORE_HEIGHT_FOR_A_FILE_IS_NOT_THE_HEIGHT_FOR_A_FRESH_RUN(monkeypatch):
    """tip-1 is correct ONLY when the keys were sampled seconds ago, and a handoff's were not.

    create_shared_wallet's own comment argues for tip-1 because "the keys were sampled seconds
    ago by this very process... every block before the current tip is provably irrelevant". That
    is --run's case. A handoff file may be HOURS old and its address already funded and buried --
    so tip-1 would put the restore height ABOVE the block holding the deposit, and the wallet
    would report 0 for a funded address.

    That failure is silent and looks exactly like the shared key not working, which is the one
    wrong conclusion this whole script exists to prevent. Hence two margins, and a test that the
    reasoning printed alongside each one is the reasoning that applies.
    """
    monkeypatch.setattr(harness, "rpc", lambda *a, **k: {"height": 2_218_209})

    fresh, tip, fresh_lines = harness.restore_height_for("node:38089", 1)
    assert (fresh, tip) == (2_218_208, 2_218_209)
    assert any("sampled seconds ago" in line for line in fresh_lines)

    from_file, _, file_lines = harness.restore_height_for("node:38089", harness.OPEN_RESTORE_MARGIN_BLOCKS)
    assert from_file == 2_218_209 - harness.OPEN_RESTORE_MARGIN_BLOCKS
    assert from_file < 2_218_209, "it must reach BELOW the tip to find a buried deposit"
    assert any("may be hours old" in line for line in file_lines)
    assert not any("sampled seconds ago" in line for line in file_lines), (
        "the --run reasoning must not be printed for the --open case; it is false there"
    )
    assert harness.OPEN_RESTORE_MARGIN_BLOCKS >= 100, (
        "the margin has to cover a realistic gap between a GRC run writing the file and the "
        "operator funding the address it names"
    )


def test_THE_WRONG_WALLET_REFUSAL_POINTS_AT_open_AND_NOT_AT_run():
    """It said "Run --run again" on the run that needed --open, 2026-09-29.

    The operator hit the wrong-wallet refusal with a swap handoff loaded and 0.01 sXMR already
    sitting at the address it names. Following that advice would have been the worst available
    action: --run SAMPLES FRESH SHARES and creates a wallet from them, replacing the one the
    fixture describes with a different address entirely -- and on a handoff that destroys the
    thing being proven, because the whole claim is that `spend_share_a` came off a chain rather
    than out of a sampler.

    The message predates --open. Adding a flag and leaving the guidance pointing elsewhere is
    the same defect this session has fixed repeatedly in other output: a screen making a claim
    the tree no longer supports.
    """
    source = pathlib.Path(harness.__file__).read_text(encoding="utf-8")
    refusal = source[source.index("REFUSING: the wallet open on port"):]
    refusal = refusal[:refusal.index("def ")]
    assert "--open" in refusal, "the flag that actually fixes it"
    assert "NOT --run" in refusal, "and an explicit warning off the one that makes it worse"
    assert "samples FRESH shares" in refusal, "with the reason, so nobody re-adds the old advice"


def test_A_WALLET_THAT_ALREADY_EXISTS_IS_OPENED_NOT_REGENERATED(monkeypatch):
    """--open failed on the second use of any wallet, which is its ORDINARY case.

    Hit on the operator's host 2026-09-29. generate_from_keys was called unconditionally and
    monero-wallet-rpc refuses an existing file:

        error::file_exists ... {'code': -1, 'message': 'Wallet already exists.'}

    --open's whole job is switching one wallet-rpc between two shared wallets that BOTH already
    exist, so this was not an edge case -- it was the second invocation, every time. And the
    docstring claimed "SAFE TO RE-RUN ... rather than making a second one": it does not make a
    second one, it FAILS, so the prose said the opposite of what the code did.

    OPENING IS HELD TO THE SAME STANDARD AS GENERATING. The address check is not skipped for an
    opened wallet -- that check is what makes trusting the file safe, and a wallet trusted
    because it was already on disk is trusted for no reason.
    """
    calls = []

    def fake(target, method, params=None, timeout=None):
        calls.append(method)
        if method == "get_info":
            return {"height": 2_218_256}
        if method == "generate_from_keys":
            raise harness.VerifyError(
                "generate_from_keys on 127.0.0.1:38084: {'code': -1, 'message': 'Wallet already exists.'}")
        if method == "open_wallet":
            return {}
        if method == "get_address":
            return {"address": "5B5yTHEEXPECTEDADDRESS"}
        raise AssertionError(method)

    monkeypatch.setattr(harness, "rpc", fake)
    monkeypatch.setattr(harness, "shared_wallet_name", lambda _a: "shared-2of2-5B5y")
    shares = {"spend_summed": 7, "view_summed": 9}

    reported = harness.create_shared_wallet(Console(), 38084, shares, "5B5yTHEEXPECTEDADDRESS", 2_217_256)
    assert reported == "5B5yTHEEXPECTEDADDRESS"
    assert "generate_from_keys" in calls and "open_wallet" in calls, (
        "it tries to create first and falls back -- not the other way round, which would open a "
        "stale file in preference to writing a correct one"
    )


def test_AN_OPENED_WALLET_WITH_THE_WRONG_ADDRESS_STILL_REFUTES(monkeypatch):
    """The fallback must not become a way to skip the check that makes it safe.

    The argument for opening rather than regenerating is that the filename is derived from the
    address, which is derived from the summed keys -- so a file of that name cannot hold
    different keys. That argument is only as good as the verification behind it, and a caller
    who removed the check would have a --open that trusts any file it happens to find.
    """
    def fake(target, method, params=None, timeout=None):
        if method == "generate_from_keys":
            raise harness.VerifyError("{'code': -1, 'message': 'Wallet already exists.'}")
        if method == "open_wallet":
            return {}
        if method == "get_address":
            return {"address": "5B5ySOMETHINGELSEENTIRELY"}
        raise AssertionError(method)

    monkeypatch.setattr(harness, "rpc", fake)
    monkeypatch.setattr(harness, "shared_wallet_name", lambda _a: "shared-2of2-5B5y")
    with pytest.raises(harness.VerifyError, match="REFUTED"):
        harness.create_shared_wallet(Console(), 38084, {"spend_summed": 7, "view_summed": 9},
                                     "5B5yTHEEXPECTEDADDRESS", 2_217_256)


def test_A_GENUINE_generate_from_keys_FAILURE_IS_NOT_SWALLOWED(monkeypatch):
    """Only "already exists" falls back. Anything else is the real failure and must surface.

    A blanket except here would turn a bad key, a dead wallet-rpc or a refused password into
    "open whatever is on disk", which is how a wallet nobody verified ends up holding a swap.
    """
    def fake(target, method, params=None, timeout=None):
        if method == "generate_from_keys":
            raise harness.VerifyError("{'code': -1, 'message': 'Failed to parse view key secret key'}")
        raise AssertionError(method)

    monkeypatch.setattr(harness, "rpc", fake)
    monkeypatch.setattr(harness, "shared_wallet_name", lambda _a: "shared-2of2-5B5y")
    with pytest.raises(harness.VerifyError, match="view key"):
        harness.create_shared_wallet(Console(), 38084, {"spend_summed": 7, "view_summed": 9},
                                     "5B5yTHEEXPECTEDADDRESS", 2_217_256)
