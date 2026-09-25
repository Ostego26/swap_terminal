"""MoneroAdapter against seeded wallet responses. No socket is opened.

Role: test (transport, seeded; no daemon, no network, no database)
Reads: chains/monero.py
Writes: nothing
Can move funds: no. The fake transport records what WOULD have been sent and
      asserts on it; nothing reaches a wallet.
Mainnet-safe: yes

WHAT THESE TESTS DO AND DO NOT ESTABLISH, stated up front because a green
suite is the thing most likely to be mistaken for evidence the adapter works.

They establish that the adapter posts to the right path, authenticates the
right way, sends NAMED parameters, converts amounts at Monero's scale, refuses
to spend when it is view-only, and turns each failure into an exception rather
than a plausible-looking empty value.

They establish NOTHING about whether `get_transfers`, `unlocked_balance` or
`subaddr_index` are what a real monero-wallet-rpc calls those things, because
the responses below are seeded by this file. See THE HONEST STATUS in
chains/monero.py: `getmonero.org` returns 403 through this environment's proxy,
so no daemon could be reached and no documentation opened. The wire format is a
hypothesis and the operator's stagenet run is the experiment.
"""

import json

import pytest
import requests
from chains.monero import MoneroAdapter, MoneroRPCError, MoneroSpendDisabled


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class Recorder:
    """Stands in for requests.post and keeps every call for assertion."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, "body": json.loads(kwargs["data"]), **kwargs})
        return FakeResponse(self.payloads.pop(0) if self.payloads else {"result": {}})

    @property
    def last_body(self):
        return self.calls[-1]["body"]


@pytest.fixture
def post(monkeypatch):
    def install(*payloads):
        recorder = Recorder(*payloads)
        monkeypatch.setattr(requests, "post", recorder)
        return recorder

    return install


def adapter(**overrides):
    settings = {"host": "127.0.0.1", "port": 18082, "user": "wallet", "password": "s3cret"}
    settings.update(overrides)
    return MoneroAdapter(**settings)


# --- transport -------------------------------------------------------------

def test_the_url_is_the_json_rpc_path_not_a_wallet_path():
    """chains/base.py builds http://host:port/wallet/<name>. Monero serves /json_rpc.

    One monero-wallet-rpc process serves exactly one wallet, chosen when the
    daemon starts, so there is no per-request wallet segment to append.
    """
    assert adapter().url == "http://127.0.0.1:18082/json_rpc"


def test_authentication_is_digest_not_basic(post):
    """base.py:86 passes a plain tuple, which requests sends as HTTP basic.

    monero-wallet-rpc authenticates with digest and would reject that. A tuple
    arriving here again means the adapter was "simplified" back into base.py's
    shape and every authenticated call will fail.
    """
    recorder = post({"result": {"valid": True}})
    adapter().validate_address("8Bsomething")
    auth = recorder.calls[-1]["auth"]
    assert isinstance(auth, requests.auth.HTTPDigestAuth)
    assert not isinstance(auth, tuple)


def test_no_auth_is_sent_when_no_user_is_configured(post):
    """`--disable-rpc-login` is a deployment choice; the adapter does not second-guess it."""
    recorder = post({"result": {"valid": True}})
    adapter(user="", password="").validate_address("8Bsomething")
    assert recorder.calls[-1]["auth"] is None


def test_parameters_are_named_not_positional(post):
    """base.py sends "params": [a, b, c]. Monero takes an object.

    A list arriving here would be accepted by requests, posted, and rejected by
    the wallet with a message about parameter parsing -- far from the cause.
    """
    recorder = post({"result": {"address": "8Bnew"}})
    adapter().get_new_address("swap-1")
    assert recorder.last_body["jsonrpc"] == "2.0"
    assert isinstance(recorder.last_body["params"], dict)
    assert recorder.last_body["params"] == {"account_index": 0, "label": "swap-1"}


def test_an_error_object_raises_and_carries_the_method_name(post):
    post({"error": {"code": -2, "message": "wallet is not opened"}})
    with pytest.raises(MoneroRPCError, match="get_balance"):
        adapter().get_balance()


def test_a_response_with_neither_result_nor_error_raises(post):
    """NOT treated as an empty answer.

    An empty deposit list reads as "no money has arrived yet" and stalls a swap
    forever without printing anything.
    """
    post({"jsonrpc": "2.0", "id": "get_balance"})
    with pytest.raises(MoneroRPCError, match="no `result` and no `error`"):
        adapter().get_balance()


# --- spending, and the refusal that is the default -------------------------

def test_send_to_address_refuses_when_view_only_and_makes_no_call(post):
    """The refusal is checked BEFORE the amount is converted and before any post.

    A misconfigured adapter must not get as far as describing a payment to a
    daemon that might be able to make it, so the assertion that matters here is
    the empty call list, not the exception.
    """
    recorder = post()
    with pytest.raises(MoneroSpendDisabled, match="view-only"):
        adapter(can_spend=False).send_to_address("8Bcustomer", 1.5)
    assert recorder.calls == []


def test_view_only_is_the_default_when_nobody_says_otherwise():
    """Fail closed. Monero is the one chain here where watching and spending separate."""
    assert MoneroAdapter(port=18082).can_spend is False


def test_send_to_address_sends_atomic_units_when_enabled(post):
    """2.5 XMR is 2500000000000 piconero, not 250000000 satoshi-scale units."""
    recorder = post({"result": {"tx_hash": "f" * 64}})
    txid = adapter(can_spend=True).send_to_address("8Bcustomer", 2.5)
    assert txid == "f" * 64
    assert recorder.last_body["method"] == "transfer"
    assert recorder.last_body["params"]["destinations"] == [
        {"address": "8Bcustomer", "amount": 2_500_000_000_000}
    ]


def test_a_transfer_with_no_tx_hash_says_the_money_may_have_gone(post):
    """The most dangerous response shape on the whole path.

    A missing field is not evidence the transfer did not happen, and the
    obvious reaction to a bare error -- retry -- pays twice. The message has to
    say so where an operator will read it.
    """
    post({"result": {"fee": 1000}})
    with pytest.raises(MoneroRPCError, match="MAY HAVE BEEN SENT"):
        adapter(can_spend=True).send_to_address("8Bcustomer", 2.5)


# --- balance ---------------------------------------------------------------

def test_get_balance_reports_unlocked_not_total(post):
    """Total includes outputs still inside the ten-block consensus lock.

    payout_service.py reads this to decide whether a payout can be funded, so
    reporting the total lets it commit to a payout the wallet cannot build.
    """
    post({"result": {"balance": 9_000_000_000_000, "unlocked_balance": 2_000_000_000_000}})
    assert adapter().get_balance() == 2.0


def test_get_balance_refuses_a_non_integer(post):
    """Reading this at the wrong scale is wrong by a factor of a trillion."""
    post({"result": {"balance": 1, "unlocked_balance": 2.0}})
    with pytest.raises(MoneroRPCError, match="not an integer"):
        adapter().get_balance()


# --- addresses -------------------------------------------------------------

def test_validate_address_returns_the_daemons_verdict(post):
    post({"result": {"valid": True, "nettype": "stagenet"}})
    assert adapter().validate_address("8Bstagenet") is True
    post({"result": {"valid": False}})
    assert adapter().validate_address("not-an-address") is False


def test_validate_address_does_not_ask_about_other_networks(post):
    """A mainnet address is syntactically perfect on a stagenet wallet.

    Leaving `any_net_type` off means the daemon's answer is scoped to the
    network it is actually on. Passing it true would validate a mainnet
    address against a stagenet wallet and the payment would fail later.
    """
    recorder = post({"result": {"valid": True}})
    adapter().validate_address("8Bsomething")
    assert "any_net_type" not in recorder.last_body["params"]


def test_validate_address_raises_rather_than_returning_false_when_it_cannot_ask(post):
    """"That address is bad" and "I could not ask" are different sentences.

    Returning False for the second shows the customer their correct address
    rejected.
    """
    post({"error": {"code": -1, "message": "daemon busy"}})
    with pytest.raises(MoneroRPCError):
        adapter().validate_address("8Bsomething")


def test_get_new_address_returns_the_subaddress(post):
    post({"result": {"address": "8Bfresh", "address_index": 7}})
    assert adapter().get_new_address("swap-42") == "8Bfresh"


def test_get_new_address_raises_and_does_not_retry(post):
    """A second derivation would orphan the first subaddress, watched by nothing."""
    recorder = post({"result": {"address_index": 7}})
    with pytest.raises(MoneroRPCError, match="NOT retried"):
        adapter().get_new_address("swap-42")
    assert len(recorder.calls) == 1


# --- deposits --------------------------------------------------------------

def test_find_deposits_returns_the_event_shape_the_application_speaks(post):
    post({"result": {"in": [{
        "txid": "b" * 64,
        "amount": 1_500_000_000_000,
        "address": "8Bswap",
        "confirmations": 11,
        "subaddr_index": {"major": 0, "minor": 4},
        "unlock_time": 0,
        "locked": False,
        "double_spend_seen": False,
        "type": "in",
    }]}})
    assert adapter().find_deposits_to_address("8Bswap") == [{
        "txid": "b" * 64, "vout": 4, "address": "8Bswap", "amount": 1.5, "confirmations": 11,
    }]


def test_find_deposits_asks_only_for_incoming_transfers(post):
    recorder = post({"result": {"in": []}})
    adapter(account_index=3).find_deposits_to_address("8Bswap")
    assert recorder.last_body["params"] == {"in": True, "account_index": 3}


def test_a_refused_scan_becomes_an_rpc_error_and_never_an_empty_list(post):
    """The conversion that would undo the whole point of monero_transfers.py.

    Turning a refusal into [] would tell the worker "no deposit yet" about a
    deposit the wallet can see.
    """
    broken = {
        "txid": "c" * 64, "amount": 1_000_000_000_000, "address": "8Bswap",
        "confirmations": 11, "unlock_time": 0, "locked": False,
        "double_spend_seen": False, "type": "in",
    }
    post({"result": {"in": [broken]}})
    with pytest.raises(MoneroRPCError, match="deposit scan for 8Bswap refused"):
        adapter().find_deposits_to_address("8Bswap")


def test_held_back_money_is_printed_rather_than_silently_absent(post, capsys):
    """Rule 14: a swap sitting at awaiting_deposit with nothing on screen is the defect."""
    post({"result": {"in": [{
        "txid": "d" * 64, "amount": 1_000_000_000_000, "address": "8Bswap",
        "confirmations": 2, "subaddr_index": {"major": 0, "minor": 1},
        "unlock_time": 0, "locked": False, "double_spend_seen": False, "type": "in",
    }]}})
    adapter().find_deposits_to_address("8Bswap")
    assert "XMR deferred" in capsys.readouterr().out


# --- configuration ---------------------------------------------------------

def test_the_confirmation_floor_is_applied_at_construction(post):
    """Not at the call site, so every reader of .min_confirmations gets the real one."""
    assert adapter(min_confirmations=2).min_confirmations == 10
    assert adapter(min_confirmations=2).configured_min_confirmations == 2
    assert adapter(min_confirmations=25).min_confirmations == 25


def test_the_banner_line_never_prints_the_password():
    """`user` and `password` are one key away in the same config dict."""
    # The suppression below is a checked claim, not a reflex: S106 flags a
    # hardcoded password, and this IS one -- a deliberately fake credential
    # whose entire purpose is to be asserted ABSENT from the rendered line two
    # statements down. The finding was read, and what it points at is the test
    # doing its job.
    line = adapter(password="hunter2", user="wallet").endpoint_line()  # noqa: S106
    assert "hunter2" not in line
    assert "wallet" not in line
    assert "127.0.0.1:18082" in line


def test_the_banner_line_says_whether_this_wallet_can_spend():
    """The difference between a watcher and an armed payout wallet, on screen."""
    assert "view-only" in adapter(can_spend=False).endpoint_line()
    assert "CAN SPEND" in adapter(can_spend=True).endpoint_line()


def test_the_banner_line_reports_a_raised_threshold_honestly():
    line = adapter(min_confirmations=2).endpoint_line()
    assert "10 blocks" in line
    assert "raised from the configured 2" in line
