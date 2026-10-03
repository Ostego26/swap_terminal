"""The real RPCAdapter with its socket replaced by a list. One implementation.

Role: test support (shared fixture class; collects no tests of its own)
Reads: chains/base.py. Nothing else, and nothing at import.
Writes: nothing
Can move funds: no. `call()` appends to a list and returns a fixed string, so
      every method of the real adapter runs and nothing leaves the process.
Mainnet-safe: yes

WHY THIS IS A SHARED MODULE AND NOT A SECOND COPY (rule 8)

tests/test_coin_amounts.py wrote this class on 2026-10-03 to assert what reaches
`sendtoaddress`, and tests/test_payout_quantization.py needed the identical
thing hours later -- for three assets instead of one, because the recording
defect it measures was found on XRP, LTC and GRC rather than on BTC alone. Two
copies of one stand-in is rule 8's shape even in test support: the copies agree
on the day they are written, and the drift is invisible because each one looks
correct in its own file.

So the survivor owns the concept and takes the asset as an argument. The move is
the same one chains/coin_amounts.amount_to_base_units() made the same day, for
the same reason and on the arrival of its second caller.

IT SUBCLASSES THE REAL ADAPTER RATHER THAN STANDING IN FOR IT, and that is the
whole value. chains/base.RPCAdapter.send_to_address() runs
fit_to_chain_precision() on whatever it is handed, and that reduction is what
several tests are about -- so a stub that merely recorded its argument would
measure the caller and skip the half that has to agree with it. Everything
except the transport is the production class.
"""

from __future__ import annotations

from chains.base import RPCAdapter

#: What `call()` answers with. A fixed string rather than a plausible txid,
#: because a test that asserted on it would be asserting on this file.
STUB_TXID = "stub-txid"


class RecordingRPCAdapter(RPCAdapter):
    """A real RPCAdapter for `asset` whose `call` records instead of connecting."""

    def __init__(self, asset: str):
        # NOT CREDENTIALS. RPCAdapter.__init__ requires a non-empty user and
        # password -- since 2026-09-26, because an empty pair is a guaranteed 401
        # and an adapter built from one is worse than no adapter -- and this
        # object never opens a socket, which this module's header states.
        super().__init__(
            user="recording-rpc-user",
            password="recording-rpc-auth-value",  # noqa: S106 -- checked: not a secret and not used; `call` is overridden below and never authenticates.
            host="127.0.0.1",
            port=18443,
        )
        self.asset = asset
        self.calls: list[tuple] = []

    def call(self, method, *params):
        self.calls.append((method, params))
        return STUB_TXID

    @property
    def sent(self) -> list[float]:
        """The amount argument of every `sendtoaddress` that reached the transport.

        A list rather than a single value, so "nothing was sent" is an assertion
        on `== []` rather than on the absence of an exception -- which is the
        distinction CLAUDE.md rule 13 draws about stops and that this
        directory's XRP tests already make about calls that must not happen.
        """
        return [params[1] for method, params in self.calls if method == "sendtoaddress"]

    def get_balance(self) -> float:
        """A balance large enough that no test is measuring a funding refusal."""
        return 10000.0
