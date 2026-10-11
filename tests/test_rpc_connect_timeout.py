"""A chain RPC's connect leg is bounded separately from its read leg, and proven on sockets.

Role: test
Reads: swap_terminal/rpc_timeout.py, and the three adapter files that call it
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- every endpoint in this file is a 127.0.0.1 socket this test
       created itself. NO CHAIN, NO NETWORK, NO WALLET. That is stated because
       CLAUDE.md's chain-safety section says a test whose network is not
       explicitly declared is assumed to be pointed at a real chain and is not
       run, and this one has no network at all.

WHAT THIS FILE PROVES, and why each assertion is a socket rather than a reading.

The fix in rpc_timeout.py splits `requests(timeout=30)` into `timeout=(5, 30)`:
five seconds to learn whether a daemon EXISTS, thirty to learn whether it has
ANSWERED. The claim being made is not "this is faster" -- it is "this is faster
on the leg where a timeout proves nothing was submitted, AND the leg where a
timeout proves nothing at all is unchanged." The second half is the safety
argument and it is the one that had to be measured, because getting it wrong
would shorten the window on a BROADCAST, where chains/icp.py:1360 and
chains/xrp.py:1832 both record that a retry after a read timeout can double-pay.

So `test_the_read_leg_survives_a_tiny_connect_leg` is the most important test
here, and it is deliberately the one that would fail if someone later "simplified"
this back to a scalar.

HOW A DROPPING ENDPOINT IS BUILT WITHOUT A FIREWALL, measured 2026-10-11.

The operator's 30s cost came from host.docker.internal's gateway SILENTLY
DROPPING the SYN -- no listener, and nothing sending RST. Three ways to produce
that locally were tried and only the third works:

  a closed port on 127.0.0.1   REFUSES. The kernel sends RST and requests raises
                               ConnectionError in 0.0014s no matter what timeout
                               is set. This was never the slow case and is kept
                               below as a baseline, so a future change that makes
                               the fast case slow gets caught.
  192.0.2.1 (TEST-NET-1,       answers ENETUNREACH in 0.0062s in a container with
  RFC 5737, never routed)      no route to it. NOT a timeout. This is why this
                               file does not use an unroutable address: it proves
                               nothing, and a test that depends on the ambient
                               network being unable to route is a test that
                               changes behavior with the host.
  a FILLED LISTEN BACKLOG      DROPS. `listen(1)` plus a dozen non-blocking
                               connects nobody accepts fills the accept queue,
                               and Linux then discards further SYNs rather than
                               refusing them (tcp_abort_on_overflow defaults to
                               0). Measured: 30.033s with a scalar `timeout=30`,
                               raising ConnectTimeout -- the SAME error class and
                               the same duration as the GRC line in the operator's
                               payout_worker.log. 5.008s with (5, 30).

The tests below use a connect leg of a QUARTER SECOND rather than the real 5.0,
so each case costs 0.25s instead of 5s. The mechanism is identical; only the
number is scaled, and the contrast test runs the scalar case at 2s so the
before/after is measured in one 2.3s test rather than asserted from the 30s run
recorded above.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import socket
import sys
import time

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "swap_terminal"))

from rpc_timeout import (
    CONNECT_TIMEOUT_SECONDS,
    _connect_seconds_from_env,
    connect_and_read,
)

REPO = pathlib.Path(__file__).resolve().parent.parent

#: The three files that post HTTP to a chain RPC endpoint, established by grepping
#: `requests.post(` across swap_terminal/chains/ rather than from memory. ICP is
#: absent on purpose: chains/icp.py runs `dfx` as a SUBPROCESS, whose single
#: `timeout=` has no connect leg to split. transactions.py is absent because it is
#: a read-only Tkinter GUI nothing imports -- see rpc_timeout.py's closing section.
HTTP_ADAPTERS = (
    "swap_terminal/chains/base.py",
    "swap_terminal/chains/solana.py",
    "swap_terminal/chains/xrp.py",
)


def code_without_prose(path: pathlib.Path) -> str:
    """One module's CODE, with every docstring and comment removed.

    THE SAME FUNCTION AS tests/test_daemon_conf.code_without_prose(), and it is
    duplicated here rather than imported for one reason: importing it would make this
    file depend on a test module whose own imports pull in chains/daemon_conf and its
    environment handling, which has nothing to do with RPC timeouts. The duplication
    is NAMED AT BOTH SITES as CLAUDE.md rule 8 requires -- see the note added to
    test_daemon_conf.py pointing here.

    Why it is needed at all: the gate below asserts that no adapter still passes
    `timeout=self.timeout`, and every one of those files now carries a COMMENT
    explaining why it does not. A raw text scan would match the comment and fail the
    file for explaining itself. That is the fifth time this exact defect has been hit
    in this tree, which is why the fix is a function and not a cleverer regex.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _refused_port() -> int:
    """A 127.0.0.1 port that will REFUSE: bound to learn the number, then closed."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _DroppingEndpoint:
    """A 127.0.0.1 port whose SYNs are DROPPED, via a filled listen backlog.

    This is the local stand-in for host.docker.internal with nothing behind it. See
    this module's docstring for the two mechanisms that do NOT work and why.
    """

    def __init__(self, backlog_fillers: int = 12):
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._server.listen(1)
        # Non-blocking so filling the queue does not block on the queue being full --
        # which is the state being created.
        self._fillers = []
        for _ in range(backlog_fillers):
            filler = socket.socket()
            filler.setblocking(False)
            # BlockingIOError IS the success case: a non-blocking connect that has not
            # completed raises it, and an incomplete connect is exactly what fills the queue.
            with contextlib.suppress(BlockingIOError):
                filler.connect(("127.0.0.1", self.port))
            self._fillers.append(filler)
        # The queue fills asynchronously; without this the first measured connect can
        # still land in a slot that has not filled yet and succeed, which would make
        # this test flaky in the direction of a FALSE PASS on the fast assertions.
        time.sleep(0.3)

    def close(self):
        for filler in self._fillers:
            filler.close()
        self._server.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _SilentListener:
    """A 127.0.0.1 port that COMPLETES the handshake and never answers.

    `listen()` without `accept()` on an unfilled queue means the kernel finishes the
    three-way handshake itself, so the connect leg succeeds in microseconds and the
    request then waits on a response that never comes. This is the only way to
    exercise the READ leg in isolation, which is what the safety claim rests on.
    """

    def __init__(self):
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]

    def close(self):
        self._server.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _elapsed_post(port: int, timeout):
    """POST to a local port and return (seconds, exception class name)."""
    started = time.monotonic()
    try:
        requests.post(f"http://127.0.0.1:{port}/", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- the EXCEPTION CLASS is the measurement. Narrowing this would mean naming the outcomes in advance, and the whole point is to report which one actually happened, including an unexpected one, so a surprising result reads as a surprising result rather than as an error in the test.
        return time.monotonic() - started, type(exc).__name__
    return time.monotonic() - started, "NO EXCEPTION"


# --- the before and after, on one dropping endpoint ------------------------------


def test_a_dropping_endpoint_costs_the_connect_leg_not_the_whole_timeout():
    """A scalar timeout spends ALL of itself on an unreachable port; a pair spends the connect leg.

    THIS IS THE OPERATOR'S 65.0s CYCLE, SHRUNK TO FIT A TEST SUITE. The real figures
    from their host on 2026-10-11 were 30.033s scalar against 5.008s paired; this runs
    the same mechanism at 2.0s against 0.25s so the contrast is measured here rather
    than quoted from a run nobody can repeat.
    """
    with _DroppingEndpoint() as endpoint:
        scalar_seconds, scalar_outcome = _elapsed_post(endpoint.port, 2.0)
    with _DroppingEndpoint() as endpoint:
        paired_seconds, paired_outcome = _elapsed_post(endpoint.port, connect_and_read(2.0, 0.25))

    assert scalar_outcome == "ConnectTimeout", (
        f"the backlog-full endpoint did not DROP, it answered with {scalar_outcome} in "
        f"{scalar_seconds:.4f}s. This test's whole mechanism depends on the SYN being "
        "discarded rather than refused -- see this module's docstring. A ConnectionError "
        "here means the kernel refused instead, and nothing below is measuring what it claims."
    )
    assert paired_outcome == "ConnectTimeout"
    assert scalar_seconds > 1.5, f"scalar timeout=2.0 returned in {scalar_seconds:.4f}s, so it did not wait"
    assert paired_seconds < 1.0, (
        f"the paired timeout spent {paired_seconds:.4f}s on a 0.25s connect leg. The split is "
        "not reaching requests -- check that the call site passes connect_and_read(...) and "
        "not a scalar."
    )


def test_the_read_leg_survives_a_tiny_connect_leg():
    """A 50ms connect leg leaves a 1s read leg running in FULL. The safety claim, measured.

    THE MOST IMPORTANT ASSERTION IN THIS FILE. The read leg is the one where a timeout
    is AMBIGUOUS on a broadcast -- the request was delivered and only the answer is
    missing, so a retry can double-pay (chains/icp.py:1360, chains/xrp.py:1832).
    Shortening it to buy speed would have traded a double-pay risk for a faster cycle.

    If a future change collapses the pair back into a scalar derived from the connect
    value, this test fails and says why. If it collapses back to the READ value, the
    test above fails instead. Between them the pair cannot quietly become one number.
    """
    with _SilentListener() as listener:
        seconds, outcome = _elapsed_post(listener.port, connect_and_read(1.0, 0.05))

    assert outcome == "ReadTimeout", (
        f"expected the read leg to expire, got {outcome} in {seconds:.4f}s. A ConnectTimeout "
        "here would mean the 0.05s connect leg is being applied to a handshake the kernel "
        "completes in microseconds, which would be the split eating the leg it must not touch."
    )
    assert seconds > 0.9, (
        f"the read leg expired after {seconds:.4f}s where 1.0s was configured. The connect "
        "value is leaking into the read leg, which on a broadcast would shorten the window "
        "in which a retry double-pays."
    )


def test_a_refused_port_stays_immediate():
    """A port that REFUSES answers in milliseconds, with or without the split.

    Kept as a baseline rather than for the fix: the refusing case was never slow, and
    measuring it here means a future change that makes it slow -- a retry loop around
    the connect, say -- gets caught by a test instead of by an operator watching a
    cycle. Measured 0.0014s; the bound below is generous by three orders of magnitude
    so a loaded CI machine does not fail it.
    """
    port = _refused_port()
    seconds, outcome = _elapsed_post(port, connect_and_read(30.0, 5.0))
    assert outcome == "ConnectionError", f"expected a refusal, got {outcome}"
    assert seconds < 1.0, (
        f"a refused port took {seconds:.4f}s. It returns RST immediately, so this is not the "
        "timeout being honored -- something is retrying or resolving."
    )


# --- the decision itself ----------------------------------------------------------


def test_the_pair_is_in_requests_own_order():
    """(connect, read), not (read, connect). Swapping them is silent and total."""
    assert connect_and_read(30.0, 5.0) == (5.0, 30.0)


def test_the_connect_leg_never_exceeds_the_read_leg():
    """A lower <ASSET>_RPC_TIMEOUT must never make the worst case LONGER.

    An operator setting GRC_RPC_TIMEOUT=1 has said "wait at most one second for
    Gridcoin." Without the min, they would get (5.0, 1.0) -- a 6s worst case from a 1s
    setting, a lower number making the thing slower. Measured on a dropping endpoint:
    the pair (1.0, 1.0) spends 1.005s, which is the setting honored.
    """
    assert connect_and_read(1.0, 5.0) == (1.0, 1.0)
    assert connect_and_read(0.25, 5.0) == (0.25, 0.25)
    # Equal is not capped away: the pair stays a pair.
    assert connect_and_read(5.0, 5.0) == (5.0, 5.0)


def test_a_configured_timeout_is_returned_unchanged_as_the_read_leg():
    """Every <ASSET>_RPC_TIMEOUT this tree ships reaches requests as the read leg, intact.

    The five 30s chains and ICP's 60 are the values config.py defines (config.py:1386,
    :1394, :1459, :1471, :1479, :1505). ICP is included even though chains/icp.py does
    not post HTTP, because stop_budget sizes the worker grace off the LONGEST of them
    and this asserts the split did not move that number.
    """
    for configured in (30.0, 60.0):
        connect, read = connect_and_read(configured, 5.0)
        assert read == configured
        assert connect == 5.0


def test_empty_and_unset_both_mean_the_default(monkeypatch):
    """A compose `environment:` entry always SETS the variable, so "" must mean "unset".

    This is the defect that made the 65s worker grace ship as 15s on 2026-10-11 --
    stop_budget._seconds_from_env() carries the full account. Here the stakes are
    higher: if "" meant 0.0, a forwarded "${ST_CHAIN_CONNECT_TIMEOUT_SECONDS:-}" would
    fail every handshake instantly and report every chain unreachable, which on the
    deposit path means a real deposit declared absent.
    """
    monkeypatch.delenv("ST_CHAIN_CONNECT_TIMEOUT_SECONDS", raising=False)
    assert _connect_seconds_from_env() == CONNECT_TIMEOUT_SECONDS
    monkeypatch.setenv("ST_CHAIN_CONNECT_TIMEOUT_SECONDS", "")
    assert _connect_seconds_from_env() == CONNECT_TIMEOUT_SECONDS
    monkeypatch.setenv("ST_CHAIN_CONNECT_TIMEOUT_SECONDS", "   ")
    assert _connect_seconds_from_env() == CONNECT_TIMEOUT_SECONDS
    monkeypatch.setenv("ST_CHAIN_CONNECT_TIMEOUT_SECONDS", "2.5")
    assert _connect_seconds_from_env() == 2.5


def test_a_non_number_refuses_rather_than_defaulting(monkeypatch):
    """`=5s` must not silently mean 5.0: that is a setting the operator believes they changed."""
    monkeypatch.setenv("ST_CHAIN_CONNECT_TIMEOUT_SECONDS", "5s")
    with pytest.raises(ValueError, match="ST_CHAIN_CONNECT_TIMEOUT_SECONDS"):
        _connect_seconds_from_env()


def test_zero_or_negative_refuses(monkeypatch):
    """Zero would report every chain unreachable, which is worse than any timeout.

    Not a theoretical input: it is what a `:-0` in a compose file, or an arithmetic
    expression that underflowed, would hand this module. A refusal at startup naming
    the variable is a defect the operator can see; a silently unreachable chain is a
    deposit declared absent.
    """
    for bad in ("0", "0.0", "-1", "-0.5"):
        monkeypatch.setenv("ST_CHAIN_CONNECT_TIMEOUT_SECONDS", bad)
        with pytest.raises(ValueError, match="positive"):
            _connect_seconds_from_env()


# --- the rule 8 gate: no fourth copy ----------------------------------------------


def test_no_http_adapter_still_passes_a_scalar_timeout():
    """None of the three HTTP adapters passes `timeout=self.timeout`, and prose cannot satisfy it.

    THIS IS THE GATE, and it is the reason this fix is not three edits that drift. The
    same scalar lived in three files with zero shared code -- CLAUDE.md rule 8's exact
    diagnosis, "two copies of one rule is not redundancy, it is a bug with a delay on
    it." A fourth adapter added later would reach for the same scalar, because that is
    what the others looked like when it was written.

    Read through code_without_prose(), because all three files now carry a COMMENT
    saying `timeout=self.timeout` in order to explain why they no longer do it. A raw
    text scan would fail a file for documenting itself -- the fifth instance of that
    defect class in this tree.
    """
    offenders = []
    for relative in HTTP_ADAPTERS:
        code = code_without_prose(REPO / relative)
        if "timeout=self.timeout" in code:
            offenders.append(relative)
    assert not offenders, (
        f"{offenders} still pass a SCALAR timeout to requests. A scalar applies to both legs, "
        "so <ASSET>_RPC_TIMEOUT=30 spends 30s establishing that a port has nothing behind it "
        "-- measured 30.033s on the operator's host 2026-10-11. Pass "
        "rpc_timeout.connect_and_read(self.timeout) instead."
    )


def test_every_http_adapter_calls_connect_and_read():
    """The mirror of the gate above: each of the three POSITIVELY calls the shared derivation.

    The negative gate alone has a hole rule 8 names directly -- an adapter that stopped
    passing a scalar by inlining its own `(5, self.timeout)` tuple would pass it while
    being a fourth copy of the decision. This asserts the shared function is what runs.
    """
    missing = [
        relative
        for relative in HTTP_ADAPTERS
        if "connect_and_read(self.timeout)" not in code_without_prose(REPO / relative)
    ]
    assert not missing, (
        f"{missing} do not call connect_and_read(self.timeout). If a chain genuinely needs a "
        "different connect leg, that belongs in rpc_timeout.py as a named case, not inlined "
        "here -- the tuple is a DECISION and rule 10 puts a decision in the smallest testable "
        "function, which is where every test above can reach it."
    )
