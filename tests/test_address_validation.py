"""A down daemon must not be reported as a bad address (rule 12's BLE001).

Role: test (read-only)
Reads: swap_terminal/chains/base.py, swap_terminal/services/swap_service.py
Writes: a throwaway SQLite database under pytest's tmp_path
Can move funds: no -- every adapter here is a stub. No socket is opened.
Mainnet-safe: yes

THE DEFECT THIS PINS.

RPCAdapter.validate_address() used to end with `except Exception: return
False`, so three different situations produced one answer:

    a genuinely malformed address        -> False
    the daemon is down / unreachable     -> False
    the daemon refused our credentials   -> False

services/swap_service.create_swap() turns False into
`ValueError("Invalid LTC payout address")`, so an outage was reported to the
operator as a customer's typo. CLAUDE.md rule 12 names this shape as the
single most expensive habit it has seen: "a broad catch is never legitimate
when the caller cannot tell the failure from a real answer."

THE SAFETY PROPERTY, WHICH IS WHAT MAKES THIS A FIX AND NOT A POSTURE CHANGE.

test_an_unreachable_daemon_still_refuses_the_swap below asserts the thing an
operator actually needs to know: the change cannot let a swap through that
would previously have been refused. Before and after, an unreachable daemon
means NO swap row is written. Only the reason changes, from a false statement
about the address to a true statement about the daemon.
"""

import pytest
from chains.base import RPCAdapter, RPCError
from db import SCHEMA, connect_db
from services.swap_service import create_swap


class StubAdapter(RPCAdapter):
    """An RPCAdapter whose `call` is a seeded script instead of a socket.

    Subclassing the REAL adapter rather than reimplementing it is deliberate:
    validate_address(), the method under test, is the real one.
    """

    asset = "LTC"

    def __init__(self, responses=None, raises=None):
        # Not a credential: this stub never opens a socket, so the values are
        # only here because RPCAdapter.__init__ requires them.
        super().__init__(user="u", password="p", host="127.0.0.1", port=19332)  # noqa: S106
        self._responses = responses or {}
        self._raises = raises or {}
        self.calls = []

    def call(self, method, *params):
        self.calls.append((method, params))
        if method in self._raises:
            raise self._raises[method]
        if method in self._responses:
            return self._responses[method]
        raise AssertionError(f"stub had no scripted answer for {method}")


CONFIG = {
    "BTC_MIN_CONFIRMATIONS": 2,
    "LTC_MIN_CONFIRMATIONS": 2,
    "GRC_MIN_CONFIRMATIONS": 6,
}


@pytest.fixture
def db(tmp_path):
    conn = connect_db(str(tmp_path / "validation.db"))
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,
                            network_fee_reserve, output_amount_estimate, expires_at, created_at)
        VALUES ('q_v', 'GRC', 'LTC', 100.0, 0.001, 150, 0.001, 0.0975, '2999-01-01T00:00:00+00:00',
                '2026-09-24T00:00:00+00:00')
        """
    )
    conn.commit()
    yield conn
    conn.close()


def test_a_valid_address_is_accepted():
    adapter = StubAdapter(responses={"validateaddress": {"isvalid": True}})
    assert adapter.validate_address("tltc1qgood") is True


def test_an_invalid_address_is_still_rejected_as_invalid():
    """The answer path is unchanged: False still means False."""
    adapter = StubAdapter(responses={"validateaddress": {"isvalid": False}})
    assert adapter.validate_address("not-an-address") is False


def test_it_falls_through_to_getaddressinfo_on_older_daemons():
    """`validateaddress` lost its wallet fields in Bitcoin Core 0.18."""
    adapter = StubAdapter(
        raises={"validateaddress": RPCError("Method not found")},
        responses={"getaddressinfo": {"isvalid": True}},
    )
    assert adapter.validate_address("tltc1qgood") is True
    assert [c[0] for c in adapter.calls] == ["validateaddress", "getaddressinfo"]


def test_an_unreachable_daemon_raises_instead_of_saying_the_address_is_bad():
    """The fix. Without it, this returns False and the test fails."""
    unreachable = ConnectionError("Connection refused")
    adapter = StubAdapter(raises={"validateaddress": unreachable, "getaddressinfo": unreachable})

    with pytest.raises(RPCError) as caught:
        adapter.validate_address("tltc1qgood")

    message = str(caught.value)
    # The message has to say what it is NOT claiming, because the operator
    # reads the screen, not the source (rule 14).
    assert "NOT a statement about the address" in message
    assert "127.0.0.1:19332" in message
    assert "Connection refused" in message


def test_an_unreachable_daemon_still_refuses_the_swap(db):
    """The safety property: this change cannot make a swap proceed.

    Before the fix, create_swap() raised ValueError("Invalid LTC payout
    address"). After it, the RPCError propagates. Both refuse, and in both
    cases the important assertion is the same one: no swap row exists.
    """
    unreachable = ConnectionError("Connection refused")
    adapters = {
        "LTC": StubAdapter(raises={"validateaddress": unreachable, "getaddressinfo": unreachable}),
        "GRC": StubAdapter(responses={"getnewaddress": "grc_deposit_addr"}),
    }

    with pytest.raises((RPCError, ValueError)):
        create_swap(db, CONFIG, adapters, "q_v", "tltc1qgood")

    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0
    # And nothing derived a deposit address either, so no wallet key was burned
    # on a swap that was never created.
    assert adapters["GRC"].calls == []


def test_a_genuinely_invalid_address_also_refuses_the_swap(db):
    """The control: the refusal path that already worked still works."""
    adapters = {
        "LTC": StubAdapter(responses={"validateaddress": {"isvalid": False}}),
        "GRC": StubAdapter(responses={"getnewaddress": "grc_deposit_addr"}),
    }

    with pytest.raises(ValueError, match="Invalid LTC payout address"):
        create_swap(db, CONFIG, adapters, "q_v", "nonsense")

    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


def test_a_good_address_still_creates_the_swap(db):
    """And the permissive path is genuinely unchanged, not merely narrowed."""
    adapters = {
        "LTC": StubAdapter(responses={"validateaddress": {"isvalid": True}}),
        "GRC": StubAdapter(responses={"getnewaddress": "grc_deposit_addr"}),
    }

    swap = create_swap(db, CONFIG, adapters, "q_v", "tltc1qgood")

    assert swap["status"] == "awaiting_deposit"
    assert swap["deposit_address"] == "grc_deposit_addr"
    assert swap["min_confirmations"] == 6  # GRC_MIN_CONFIRMATIONS, in blocks
    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 1
