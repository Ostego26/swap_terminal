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

import gridcoin_credentials
import identity
import pytest


def test_an_unconfigured_call_raises_its_own_error_type(monkeypatch):
    """Not a bare RuntimeError, and not the daemon's HTTPError.

    "The wallet said no" and "you did not configure me" are different
    diagnoses, and an operator reads the traceback.
    """
    monkeypatch.delenv("GRIDCOIN_RPC_PASSWORD", raising=False)
    monkeypatch.delenv("GRIDCOIN_RPC_PASS", raising=False)
    monkeypatch.delenv("RPC_PASS", raising=False)
    with pytest.raises(gridcoin_credentials.GridcoinCredentialsMissing):
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

    monkeypatch.delenv("GRIDCOIN_RPC_PASSWORD", raising=False)
    monkeypatch.delenv("GRIDCOIN_RPC_PASS", raising=False)
    monkeypatch.delenv("RPC_PASS", raising=False)
    monkeypatch.setattr(identity.requests, "post", explode)
    with pytest.raises(gridcoin_credentials.GridcoinCredentialsMissing):
        identity.rpc_call("sendrawtransaction", ["deadbeef"])


def test_the_refusal_names_the_method_and_which_chain_the_default_is(monkeypatch):
    """Rule 14: say what the number means, next to the number.

    25779 and 15715 differ by four characters and by whether the coins are
    real. An operator setting a password for the first time should be told
    which one this module is pointed at, in the message, rather than having to
    open the file.
    """
    monkeypatch.delenv("GRIDCOIN_RPC_PASSWORD", raising=False)
    monkeypatch.delenv("GRIDCOIN_RPC_PASS", raising=False)
    monkeypatch.delenv("RPC_PASS", raising=False)
    with pytest.raises(gridcoin_credentials.GridcoinCredentialsMissing) as caught:
        identity.rpc_call("sendrawtransaction")
    message = str(caught.value)
    assert "sendrawtransaction" in message
    # The CLAIM, not the phrasing: that nothing was attempted and no socket
    # opened. An earlier version of this test asserted the literal words "NOT
    # sent" and broke when the message was reworded -- pinning prose rather
    # than behavior, which is a test that costs maintenance and catches nothing.
    assert "NOT attempted" in message
    assert "no socket was opened" in message
    assert "25779" in message and "15715" in message


def test_no_hardcoded_password_survives_as_a_default():
    """The default is empty, not a value.

    A non-empty default here is the defect returning, whatever the value is --
    so this asserts the shape rather than matching any particular string, which
    would only catch the exact password that was already removed.
    """
    for name in gridcoin_credentials.GRIDCOIN_PASSWORD_VARIABLES:
        if os.environ.get(name):
            pytest.skip(f"{name} is set in this environment, so the default is not observable")
    assert gridcoin_credentials.gridcoin_rpc_password() == ""


# ---------------------------------------------------------------------------
# The resolver itself: four names for one secret (see gridcoin_credentials.py).
# ---------------------------------------------------------------------------

def test_the_name_the_operators_env_actually_uses_is_resolved(monkeypatch):
    """THE BUG THIS MODULE WAS WRITTEN FOR.

    The live .env on the operator's host spells it GRIDCOIN_RPC_PASSWORD, and
    identity.py/chain_tx.sh read GRIDCOIN_RPC_PASS. Neither script could have
    authenticated against the configuration that exists, and the symptom would
    have been a 401 reading as "the wallet is broken".
    """
    for name in gridcoin_credentials.GRIDCOIN_PASSWORD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRIDCOIN_RPC_PASSWORD", "from-the-dotenv")
    assert gridcoin_credentials.gridcoin_rpc_password() == "from-the-dotenv"


def test_every_known_spelling_resolves(monkeypatch):
    """All three, one at a time, so none is silently dropped from the tuple."""
    for candidate in gridcoin_credentials.GRIDCOIN_PASSWORD_VARIABLES:
        for name in gridcoin_credentials.GRIDCOIN_PASSWORD_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(candidate, f"value-via-{candidate}")
        assert gridcoin_credentials.gridcoin_rpc_password() == f"value-via-{candidate}"


def test_the_most_explicit_name_wins_when_several_are_set(monkeypatch):
    """A host with both set is saying the more specific one is what it means."""
    monkeypatch.setenv("GRIDCOIN_RPC_PASSWORD", "specific")
    monkeypatch.setenv("GRIDCOIN_RPC_PASS", "less-specific")
    monkeypatch.setenv("RPC_PASS", "least-specific")
    assert gridcoin_credentials.gridcoin_rpc_password() == "specific"


def test_an_empty_value_counts_as_unset_and_falls_through(monkeypatch):
    """THE FAILURE THAT PROMPTED THIS RULE, on 2026-09-25.

    A rotation script called openssl, openssl failed for a missing shared
    library, and the unchecked empty result was written into a live .env --
    leaving GRIDCOIN_RPC_PASSWORD set to "". Treating that as configured would
    authenticate with nothing and report the daemon as broken, which is the
    most expensive way to present a missing password.
    """
    monkeypatch.setenv("GRIDCOIN_RPC_PASSWORD", "")
    monkeypatch.setenv("GRIDCOIN_RPC_PASS", "the-real-one")
    assert gridcoin_credentials.gridcoin_rpc_password() == "the-real-one"

    monkeypatch.setenv("GRIDCOIN_RPC_PASS", "")
    monkeypatch.delenv("RPC_PASS", raising=False)
    assert gridcoin_credentials.gridcoin_rpc_password() == ""


def test_grc_rpc_pass_is_deliberately_not_resolved(monkeypatch):
    """It belongs to the Flask app's per-chain vocabulary, not to these scripts.

    Pinned so that "helpfully" adding it later is a deliberate change rather
    than a drive-by, and so a reader who greps for it finds the reason.
    """
    assert "GRC_RPC_PASS" not in gridcoin_credentials.GRIDCOIN_PASSWORD_VARIABLES
    for name in gridcoin_credentials.GRIDCOIN_PASSWORD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRC_RPC_PASS", "the-flask-apps-credential")
    assert gridcoin_credentials.gridcoin_rpc_password() == ""


def test_transactions_no_longer_carries_a_default_password():
    """The second instance of the defect, found only by a wider grep.

    transactions.py read os.getenv("GRIDCOIN_RPC_PASSWORD", "changeme") --
    identical in shape to identity.py's, and missed when that one was fixed.
    """
    # Imported HERE, not at the top, and PLC0415 is suppressed with the reason:
    # transactions.py does `import tkinter` at module level, which is absent on
    # any host without python3-tk (its own requirements.txt documents this).
    # A top-level import would fail COLLECTION for the entire suite on a
    # headless machine -- turning one skipped test into 500 that never ran.
    pytest.importorskip("tkinter", reason="transactions.py imports tkinter at module level")
    import transactions  # noqa: PLC0415 -- checked: see above; a module-level import breaks collection headless.

    for name in gridcoin_credentials.GRIDCOIN_PASSWORD_VARIABLES:
        if os.environ.get(name):
            pytest.skip(f"{name} is set in this environment, so the default is not observable")
    assert transactions.RPC_PASSWORD == ""
