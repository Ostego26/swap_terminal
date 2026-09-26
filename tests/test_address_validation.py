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


# --- a chain with no adapter in this process ----------------------------------
#
# These sit beside the address tests because they guard the SAME line: create_swap()
# reaches adapters[to_asset] to validate the payout address, and on 2026-09-26 that
# subscript is what failed. The operator saw
#
#     No swap was created: 'GRC'
#
# on the page -- str(KeyError("GRC")) and nothing more. They had a Gridcoin testnet
# daemon on 25715 and three workers printing `GRC rpc=127.0.0.1:25715`; the SERVER
# process had no GRC_RPC_PORT, so build_adapters() skipped Gridcoin. Every fact
# needed to fix it was one variable name, and none of it reached the screen.


def test_a_chain_with_no_adapter_refuses_and_names_the_variable(db):
    """MUTATION: delete the unconfigured_chains() guard. The message becomes 'LTC'."""
    adapters = {"GRC": StubAdapter(responses={"getnewaddress": "Sgrcaddr"})}

    with pytest.raises(ValueError) as caught:
        create_swap(db, CONFIG, adapters, "q_v", "tltc1qgood")

    message = str(caught.value)
    assert "LTC" in message
    assert "LTC_RPC_PORT" in message, "the operator needs the variable, not the key's repr"
    assert ".env" in message, "and that a value in a file only does not reach the process"
    assert message != "'LTC'", "this is the exact string the defect produced"


def test_the_refusal_writes_no_swap_row(db):
    """A refusal that leaves a row behind is worse than no refusal.

    A tag-attributed swap with no deposit_tag credits NOTHING
    (services/deposit_service.attributable_events), so a half-written row is a swap
    that can be paid into and never advanced.
    """
    adapters = {"GRC": StubAdapter(responses={"getnewaddress": "Sgrcaddr"})}

    with pytest.raises(ValueError):
        create_swap(db, CONFIG, adapters, "q_v", "tltc1qgood")

    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM xrp_destination_tags").fetchone()["n"] == 0


def test_both_missing_chains_are_named_in_one_refusal(db):
    """One refusal naming both beats two consecutive single-chain failures.

    The source chain matters as much as the destination: a swap whose FROM chain
    has no adapter has no deposit watcher looking at it either, so it would sit at
    awaiting_deposit forever with a deposit address nothing polls.
    """
    with pytest.raises(ValueError) as caught:
        create_swap(db, CONFIG, {}, "q_v", "tltc1qgood")

    message = str(caught.value)
    assert "GRC_RPC_PORT" in message, "the source chain must be named too"
    assert "LTC_RPC_PORT" in message
    assert "Nothing was written." in message


def test_the_refusal_says_why_the_quote_priced_anyway(db):
    """The operator's actual confusion: the quote worked, so what changed?

    ALLOWED_PAIRS is what the terminal is WILLING to swap and the adapters are what
    it can REACH. create_quote() only consults the first, which is why 1 XRP priced
    at 56.6 GRC on a server that could not reach Gridcoin at all.
    """
    with pytest.raises(ValueError) as caught:
        create_swap(db, CONFIG, {}, "q_v", "tltc1qgood")

    message = str(caught.value)
    assert "ALLOWED_PAIRS" in message
    assert "GRC->LTC" in message, "name the pair, so the message stands alone when pasted"


# --- a destination that cannot pay out ----------------------------------------
#
# "REACHABLE" AND "ABLE TO PAY" ARE DIFFERENT QUESTIONS, and create_swap() is where
# the second one has to be asked. routes/ui.py stops OFFERING such a pair, but a
# POST to /api/swaps does not come from the page.
#
# GRC -> XRP, 2026-09-26: an XRP adapter exists and reaches the testnet, so the
# unconfigured check passes. XRPAdapter holds no signing key and payout_service
# calls send_to_address() unarmed, so the payout RAISES -- the customer's GRC would
# be taken, credited, and the swap left in `failed` needing a person. These two
# tests were added after a mutation run showed that removing the guard failed
# NOTHING: the page had tests, the authority did not.


def cannot_pay_quote(db):
    """A GRC -> XRP quote, so to_asset is a chain that cannot be a destination."""
    db.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q_np','GRC','XRP',1.0,0.02,150,0.0,0.0195,'2999-01-01T00:00:00+00:00',"
        " '2026-09-26T00:00:00+00:00')"
    )
    db.commit()


class PayingAdapter:
    """A destination that CAN pay out. Declared, because the check fails closed.

    NOT a subclass of StubAdapter: that one records RPC calls and answers only from
    a script, so validate_address() would go through the real RPCAdapter path and
    raise RPCError for a method nobody scripted -- which is the right behavior for
    the tests above and the wrong harness for these, where the address is incidental
    and the payout capability is the subject.
    """

    can_spend = True
    payout_refusal = ""

    def validate_address(self, address):
        return bool(address)

    def get_new_address(self, label):
        return f"fresh-address-for-{label}"


class ViewOnlyAdapter:
    """Reachable, and cannot be paid out from. What XRPAdapter and SolanaAdapter are."""

    can_spend = False
    payout_refusal = "cannot pay out: this adapter holds no signing key."

    def validate_address(self, address):
        raise AssertionError(
            "validate_address must NOT be reached for a destination that cannot pay out -- "
            "the capability check runs first, precisely because XRPAdapter's validator "
            "accepts any X-address without verifying its checksum"
        )


def test_a_destination_that_cannot_pay_out_refuses_the_swap(db):
    """MUTATION: delete the why_cannot_pay_out() call in create_swap(). Only this
    test and the one below fail -- the page's tests do not cover the API."""
    cannot_pay_quote(db)
    adapters = {"GRC": PayingAdapter(), "XRP": ViewOnlyAdapter()}

    with pytest.raises(ValueError) as caught:
        create_swap(db, CONFIG, adapters, "q_np", "rBfM7je6e9Ca2cMvuRn7cr9xExFgDa5NGx")

    message = str(caught.value)
    assert "cannot pay out" in message
    assert "GRC->XRP" in message, "name the pair, so the message stands alone when pasted"
    assert "Nothing was written." in message
    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


def test_an_adapter_that_declares_nothing_is_treated_as_unable_to_pay(db):
    """FAIL-CLOSED, and the mutation that reverses it failed no test until now.

    chains/registry.why_cannot_pay_out() reads getattr(adapter, "can_spend", False).
    The other default -- assume it can pay -- means a NEW adapter that forgets the
    declaration is silently offered as a destination and strands the first deposit
    into it. That is the expensive direction, so the silent one must be the safe one.
    """
    cannot_pay_quote(db)

    class Undeclared:
        """No can_spend, no payout_refusal. Exactly what a new adapter looks like."""

        def validate_address(self, address):
            return True

    with pytest.raises(ValueError, match="cannot pay out"):
        create_swap(db, CONFIG, {"GRC": PayingAdapter(), "XRP": Undeclared()}, "q_np", "raddress")

    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 0


def test_a_destination_that_can_pay_out_is_not_refused_by_this_check(db):
    """So the guard cannot pass by refusing everything.

    Uses the fixture's own GRC -> LTC quote, whose destination declares can_spend.
    """
    adapters = {"GRC": PayingAdapter(), "LTC": PayingAdapter()}

    swap = create_swap(db, CONFIG, adapters, "q_v", "tltc1qgood")

    assert swap["to_asset"] == "LTC"
    assert db.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"] == 1


# --- is this address ours? ------------------------------------------------------
#
# THREE ANSWERS, AND None IS NOT A FAILURE. `ismine` is a WALLET field, and Bitcoin
# Core moved wallet fields out of validateaddress into getaddressinfo in 0.18 -- so a
# daemon can answer "is this well-formed" and not "is this yours". A caller that read
# None as False would print the reassuring answer about the one fact that decides how
# to read a payout: on 2026-09-26 the operator's payout went to their OWN address, the
# net movement was the fee, and they read a working swap as a broken one.
#
# These were added after a mutation run: making owns_address() return False instead of
# None when unanswerable failed NOTHING, because every test went through open_swap's
# stubs rather than through this method.


def test_owns_address_is_true_when_the_daemon_says_ismine():
    adapter = StubAdapter(responses={"getaddressinfo": {"isvalid": True, "ismine": True}})

    assert adapter.owns_address("Sgrcaddr") is True


def test_owns_address_is_false_when_the_daemon_says_not_ismine():
    adapter = StubAdapter(responses={"getaddressinfo": {"isvalid": True, "ismine": False}})

    assert adapter.owns_address("Sgrcaddr") is False


def test_owns_address_is_none_when_no_method_reports_ismine():
    """MUTATION: return False here instead of None. This test alone fails, and it is
    the distinction the caller depends on."""
    adapter = StubAdapter(responses={
        "getaddressinfo": {"isvalid": True},
        "validateaddress": {"isvalid": True},
    })

    assert adapter.owns_address("Sgrcaddr") is None


def test_owns_address_is_none_when_the_daemon_cannot_be_asked():
    """An outage is NOT "not yours". Same reasoning as validate_address() raising
    rather than returning False -- see its docstring for the 2026-09-24 incident."""
    adapter = StubAdapter(raises={
        "getaddressinfo": RPCError("connection refused"),
        "validateaddress": RPCError("connection refused"),
    })

    assert adapter.owns_address("Sgrcaddr") is None


def test_owns_address_prefers_getaddressinfo():
    """Bitcoin Core 0.18 moved wallet fields there, so it is asked first. A daemon
    still carrying ismine on validateaddress is answered by the fallback."""
    modern = StubAdapter(responses={"getaddressinfo": {"ismine": True}})
    older = StubAdapter(responses={
        "getaddressinfo": {"isvalid": True},
        "validateaddress": {"isvalid": True, "ismine": True},
    })

    assert modern.owns_address("Sgrcaddr") is True
    assert older.owns_address("Sgrcaddr") is True


def test_owns_address_never_reaches_a_key():
    """Read-only: the only RPCs it may call are the two validity ones."""
    adapter = StubAdapter(responses={"getaddressinfo": {"ismine": False}})

    adapter.owns_address("Sgrcaddr")

    assert [method for method, _params in adapter.calls] == ["getaddressinfo"]
