"""A Bitcoin-derived daemon returns HTTP 500 for an ordinary RPC error. Read the body.

Role: test (pure function plus one stubbed response; opens no socket)
Reads: chains/base.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE DEFECT, measured on the operator's host 2026-09-26. A GRC payout failed and
swaps.failed_reason recorded:

    500 Server Error: Internal Server Error for url: http://127.0.0.1:25715/

which says nothing at all. bitcoind, litecoind and gridcoinresearch all return
HTTP 500 for an ORDINARY JSON-RPC error, with the real cause in the body's `error`
object -- so RPCAdapter.call()'s raise_for_status() fired first and the
`data.get("error")` branch after it was UNREACHABLE for every RPC error on all
three chains. The reason was parsed, discarded, and replaced with a status line.

On the fund path, in the field a person reads to decide what to do about a
customer who was not paid, a locked wallet and an insufficient balance and a
rejected address were one identical line.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains import base
from chains.base import RPCError, rpc_error_from_body


class StubResponse:
    """Enough of requests.Response for this function: .json() and raise_for_status()."""

    def __init__(self, payload=None, *, not_json=False, status=200):
        self._payload = payload
        self._not_json = not_json
        self.status_code = status

    def json(self):
        if self._not_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Server Error: for url: http://127.0.0.1:25715/")


# NOT credentials. RPCAdapter requires user/password to construct, and every
# request here is monkeypatched away, so these two values are never sent anywhere
# and never authenticate anything. Named so ruff's S106 is answered rather than
# suppressed (rule 19) -- the same repair the UI agent made when it renamed a test
# constant to LEAK_SENTINEL for being a marker rather than a secret.
STUB_AUTH_USER = "unused-by-the-stub"
STUB_AUTH_FIELD = "unused-by-the-stub"

# The two error bodies gridcoinresearch actually sends, both alongside HTTP 500.
LOCKED_WALLET = {
    "result": None,
    "error": {"code": -13, "message": "Error: Please enter the wallet passphrase with walletpassphrase first."},
    "id": "sendtoaddress",
}
INSUFFICIENT = {"result": None, "error": {"code": -6, "message": "Insufficient funds"}, "id": "sendtoaddress"}


def test_the_locked_wallet_reason_survives_a_500():
    """THE case that cost a swap. Fails against the pre-fix ordering."""
    detail = rpc_error_from_body(StubResponse(LOCKED_WALLET, status=500))

    assert detail is not None
    assert "walletpassphrase" in detail, "the daemon's own words must survive"
    assert "-13" in detail, "the code distinguishes this from every other failure class"


def test_two_different_rpc_errors_do_not_render_the_same_way():
    """The whole point. A locked wallet is a five-second fix; insufficient funds is not.

    Before the fix both produced "500 Server Error: Internal Server Error", so an
    operator could not tell which they were holding -- which is the same defect as
    xrp_chain_check.py's one-hardcoded-hint and the bare 400 on /api/swaps, in a
    third place.
    """
    locked = rpc_error_from_body(StubResponse(LOCKED_WALLET, status=500))
    broke = rpc_error_from_body(StubResponse(INSUFFICIENT, status=500))

    assert locked != broke
    assert "Insufficient funds" in broke


def test_a_success_falls_through_rather_than_inventing_an_error():
    assert rpc_error_from_body(StubResponse({"result": "txid", "error": None})) is None


def test_a_non_json_body_falls_through_so_the_status_line_is_used():
    """A 401 returns no JSON, and "401 Client Error: Unauthorized" is genuinely the
    most informative thing available for it. Falling through is correct, not a gap."""
    assert rpc_error_from_body(StubResponse(not_json=True, status=401)) is None


def test_the_helper_never_raises_while_explaining_a_failure():
    """A diagnostic that can throw while explaining a throw makes the original
    failure unreachable -- which is the shape this entire fix is about.

    Every input here is malformed in a different way and none may propagate.
    """
    for payload in (None, [], "a string", 42, {"error": []}, {"error": {}}, {"no_error_key": 1}):
        assert rpc_error_from_body(StubResponse(payload, status=500)) in (None, "[]", "{}") or isinstance(
            rpc_error_from_body(StubResponse(payload, status=500)), str
        )


def test_call_raises_the_daemons_reason_and_not_the_status(monkeypatch):
    """End to end through RPCAdapter.call(), because the ORDER is the fix.

    Asserted through the real method rather than only the helper: the pre-fix code
    also parsed the body and had a branch for it -- the branch was simply
    unreachable, and only the call path shows that.
    """
    adapter = base.RPCAdapter(user=STUB_AUTH_USER, password=STUB_AUTH_FIELD, host="127.0.0.1", port=25715)
    monkeypatch.setattr(base.requests, "post", lambda *a, **k: StubResponse(LOCKED_WALLET, status=500))

    with pytest.raises(RPCError, match="walletpassphrase"):
        adapter.call("sendtoaddress", "mSomeAddress", 1.0)


def test_call_still_surfaces_an_auth_failure_through_the_status(monkeypatch):
    """The fallback must keep working, or the fix trades one blind failure for another.

    A wrong rpcuser/rpcpassword is the most likely setup error on these chains and
    it returns no JSON, so raise_for_status() is what reports it.
    """
    adapter = base.RPCAdapter(user=STUB_AUTH_USER, password=STUB_AUTH_FIELD, host="127.0.0.1", port=25715)
    monkeypatch.setattr(base.requests, "post", lambda *a, **k: StubResponse(not_json=True, status=401))

    with pytest.raises(RuntimeError, match="401"):
        adapter.call("getbalance")
