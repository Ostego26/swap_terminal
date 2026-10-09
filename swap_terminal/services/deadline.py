#!/usr/bin/env python3
"""How long the next outbound call may take, when a whole request has a budget.

Role: submodule (a pure decision; it reads nothing and calls nothing)
Reads: nothing
Writes: nothing
Can move funds: no
Live-safe: yes

WHY THIS IS ITS OWN FILE RATHER THAN A HELPER IN ITS FIRST CALLER (rule 8). It
was written inside services/pricing.py on 2026-10-08 and had a second caller the
same day -- services/admin_view.probe_chains(). Two copies of one rule agree on
the day they are written and drift from then on, and the drift here would be
invisible: each copy looks correct in its own file and nothing fails until one
of them lets a request outrun the server.

THE DEFECT CLASS, WHICH BOTH CALLERS HAD INDEPENDENTLY. A request makes N
outbound calls, each with a per-call timeout that is defensible on its own, and
nobody adds them up against the WORKER timeout:

    prices    1 CoinGecko call  x 15s  +  6 CoinPaprika calls x 15s  = 105s
    chains    6 chains x up to 2 RPCs x 30s                          = 360s
    gunicorn  timeout                                                   60s

Measured on the operator's host 2026-10-08: `GET /api/admin/chains` returned
HTTP 500 after 60.18s with an EMPTY BODY. That is not a chain answering slowly,
it is gunicorn killing the worker at its timeout -- and because probe_chains()
built its whole list before returning, the operator learned nothing about which
of the six chains was unreachable. A diagnostic that dies is worse than one that
is slow: it costs a worker AND answers nothing.

THE RULE THIS ENCODES: a per-call timeout bounds a call, and only a deadline
bounds a request.
"""

from __future__ import annotations

#: Below this much remaining budget, the next call is NOT started.
#:
#: Starting a request with half a second left buys a guaranteed timeout and
#: spends the half second doing it. Worse, it fails in a way indistinguishable
#: from the endpoint being down, so the caller's own bookkeeping gets reported as
#: somebody else's outage. Refusing to start is the same answer sooner, and it is
#: the difference between a budget and a formality.
MINIMUM_USEFUL_CALL_SECONDS = 1.0


def call_timeout(deadline: float, now: float, per_call: float) -> float:
    """How long the next call may take. 0.0 means DO NOT START IT. Pure.

    `per_call` is still respected -- the budget is a CEILING, not a replacement.
    A 15s call with 40s of budget left still gets 15s, so a single slow endpoint
    behaves exactly as it does today and only the TOTAL is bounded.

    The thing that goes wrong with a budget is always arithmetic: letting the
    last call run with its full per-call timeout puts the total back over the
    server's limit, and nothing about the code looks different when it does.
    That is why this is a named function with its own tests rather than a
    `min()` inlined at four call sites.
    """
    remaining = deadline - now
    if remaining <= MINIMUM_USEFUL_CALL_SECONDS:
        return 0.0
    return min(per_call, remaining)
