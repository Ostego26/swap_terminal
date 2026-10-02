"""The seeded rippled transport: one recorder, one server_info, one account_info.

Role: test support (shared fixtures; collects no tests of its own)
Reads: nothing. Every value here is a literal measured off a real server and
      written down, and nothing in this file opens a socket.
Writes: nothing
Can move funds: no. This file's whole purpose is to stand in for the socket so
      that the real adapter code can be run with nothing reachable.
Mainnet-safe: yes

WHY THIS IS A SHARED MODULE AND NOT A SECOND COPY (rule 8)

tests/test_xrp_adapter.py wrote these four helpers on 2026-09-26 and
tests/test_xrp_payout_wiring.py needs the identical ones on 2026-10-02. Two
copies of one rule is a bug with a delay on it, and the delay here would be
unusually expensive because THESE PARTICULAR VALUES WERE MEASURED and the
measurements are the point:

    info.validated_ledger.reserve_base_xrp   1      a NUMBER, not a string
    info.validated_ledger.reserve_inc_xrp    0.2
    info.network_id                          1      on s.altnet.rippletest.net
    account_data.Balance                     "100000000"   a STRING, because the
                                             ledger sends drop counts as strings
                                             so a JavaScript client cannot round
                                             them through a double

All of those were confirmed against rippled 3.4.1 from the operator's host on
2026-09-25 and 2026-09-26. A second copy that seeded `Balance` as an int would
pass against a parser that mishandles the real shape, and would keep passing --
which is the failure mode where a test file makes a broken reader look correct.

So this file is the survivor and it owns the concept. test_xrp_adapter.py's own
module docstring still records what its tests establish and what they do not;
only the four helpers moved, with no change to any value.
"""

from __future__ import annotations

import json

TESTNET_URL = "https://s.altnet.rippletest.net:51234/"
MAINNET_URL = "https://s1.ripple.com:51234/"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class Recorder:
    """Stands in for requests.post, answering rippled calls by METHOD NAME.

    Keyed by method rather than ordered, unlike an ordered queue, because the
    order of server_info and account_info is itself under test: a queue would
    answer whichever call came first with the server_info payload and the
    assertions would pass for the wrong reason.

    It RECORDS as well as answers, and that is not convenience: half the
    assertions across both files are about calls that must NOT have happened.
    "The exception was raised" does not distinguish a guard that refused before
    reading the account from one that refused after -- the call list does. It is
    also how "nothing reached the network at all" becomes an assertion on an
    empty list rather than on the absence of an error.
    """

    def __init__(self, **by_method):
        self.by_method = by_method
        self.calls = []

    def __call__(self, url, **kwargs):
        body = json.loads(kwargs["data"])
        self.calls.append({"url": url, "method": body["method"], "params": body["params"]})
        return FakeResponse({"result": self.by_method.get(body["method"], {"status": "success"})})

    @property
    def methods(self):
        return [call["method"] for call in self.calls]


def server_info(network_id=1, base_reserve=1, owner_reserve=0.2, base_fee=None):
    """A server_info result in the shape confirmed live on 2026-09-25.

    reserve_base_xrp came back as a NUMBER (1) and not a string, network_id as 1
    on s.altnet.rippletest.net. base_fee_xrp is None by default here precisely
    because it was NOT among the fields confirmed that day -- so the default
    path through these tests is the one where the adapter falls back to
    xrp_signing.FEE_ALLOWANCE_DROPS and says so.
    """
    ledger = {"reserve_base_xrp": base_reserve, "reserve_inc_xrp": owner_reserve}
    if base_fee is not None:
        ledger["base_fee_xrp"] = base_fee
    return {"status": "success", "info": {"network_id": network_id, "build_version": "3.4.1", "validated_ledger": ledger}}


def account_info(drops="100000000", owner_count=0):
    """100 XRP by default -- the XRPL testnet faucet's own grant, measured 2026-09-26.

    Balance is a STRING because that is how the ledger sends drop counts, so a
    JavaScript client cannot round them through a double. A test that seeded it
    as an int would pass against a parser that mishandles the real shape.
    """
    data = {"Balance": drops}
    if owner_count is not None:
        data["OwnerCount"] = owner_count
    return {"status": "success", "account_data": data}
