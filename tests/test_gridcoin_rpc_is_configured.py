"""identity.py refuses to make an RPC call with no password configured.

Role: test (no socket is opened; the stub fails the test if one would be)
Reads: identity.py
Writes: nothing
Can move funds: no. What it pins is a REFUSAL on a path that can broadcast.
Mainnet-safe: yes

WHY THIS EXISTS

Until 2026-09-25, GRIDCOIN_RPC_PASS had a hardcoded password as its default,
duplicated from chain_tx.sh. Both were removed, and removing a default is the
kind of change that gets quietly undone by the next person who finds the script
inconvenient to configure -- so the refusal is pinned here rather than left to
a comment.

The point is not that the password is secret. It was a testnet credential and
nothing of value was behind it. The point is that a default like that makes the
insecure path the SILENT one: a reader who never sets the variable gets a
working script and no signal, right up until they point it at a chain that is
not testnet.
"""

import os

import identity
import pytest


def test_an_unconfigured_call_raises_its_own_error_type(monkeypatch):
    """Not a bare RuntimeError, and not the daemon's HTTPError.

    "The wallet said no" and "you did not configure me" are different
    diagnoses, and an operator reads the traceback.
    """
    monkeypatch.setattr(identity, "GRIDCOIN_RPC_PASS", "")
    with pytest.raises(identity.GridcoinRPCNotConfigured):
        identity.rpc_call("getinfo")


def test_an_unconfigured_call_opens_no_socket(monkeypatch):
    """THE ASSERTION THAT MATTERS.

    rpc_call carries sendrawtransaction. An unauthenticated call returns HTTP
    401, which raise_for_status() turns into a generic HTTPError several frames
    from the cause -- and the obvious reaction to an unexplained failure on a
    broadcast is to retry. So the check must happen BEFORE the request, and
    this stub turns any request at all into a failure.
    """
    def explode(*args, **kwargs):
        raise AssertionError("a request was made despite no password being configured")

    monkeypatch.setattr(identity, "GRIDCOIN_RPC_PASS", "")
    monkeypatch.setattr(identity.requests, "post", explode)
    with pytest.raises(identity.GridcoinRPCNotConfigured):
        identity.rpc_call("sendrawtransaction", ["deadbeef"])


def test_the_refusal_names_the_method_and_which_chain_the_default_is(monkeypatch):
    """Rule 14: say what the number means, next to the number.

    25779 and 15715 differ by four characters and by whether the coins are
    real. An operator setting a password for the first time should be told
    which one this module is pointed at, in the message, rather than having to
    open the file.
    """
    monkeypatch.setattr(identity, "GRIDCOIN_RPC_PASS", "")
    with pytest.raises(identity.GridcoinRPCNotConfigured) as caught:
        identity.rpc_call("sendrawtransaction")
    message = str(caught.value)
    assert "sendrawtransaction" in message
    assert "NOT sent" in message
    assert "25779" in message and "15715" in message


def test_no_hardcoded_password_survives_as_a_default():
    """The default is empty, not a value.

    A non-empty default here is the defect returning, whatever the value is --
    so this asserts the shape rather than matching any particular string, which
    would only catch the exact password that was already removed.
    """
    monkey_free = os.environ.get("GRIDCOIN_RPC_PASS")
    if monkey_free:
        pytest.skip("GRIDCOIN_RPC_PASS is set in this environment, so the default is not observable")
    assert identity.GRIDCOIN_RPC_PASS == ""
