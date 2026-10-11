"""How long to wait for a chain RPC endpoint, split into its two legs.

Role: submodule (a derivation; one decision and the constants it reads)
Reads: ST_CHAIN_CONNECT_TIMEOUT_SECONDS from the environment
Writes: nothing
Can move funds: no -- it returns a pair of numbers. It is READ BY the fund path
       (chains/base.py, chains/solana.py, chains/xrp.py all pass its result to
       requests), so a wrong answer here can make a broadcast report
       unreachable. See the safety argument below for why the leg this module
       shortens is the one where that is harmless by construction.
Mainnet-safe: yes

WHY THIS MODULE EXISTS, measured on the operator's host 2026-10-11.

A payout cycle took 65.0s to do nothing. The worker log said so in its own
words, and the figure is the whole reason this file is here:

    payout_worker cycle=1 IDLE   pending_at_start=0 broadcast=0 failed_total=8
                                 in 53.7µfn (65.0s)

The line above it named the cause:

    wallet inventory for GRC NOT refreshed (previous values kept) ...
    ConnectTimeoutError(... 'Connection to host.docker.internal timed out.
    (connect timeout=30.0)')

THIRTY SECONDS TO LEARN THAT A PORT HAS NOTHING BEHIND IT. `requests` applies a
SCALAR `timeout=` to BOTH of its legs -- the TCP/TLS handshake and the wait for
the response body -- so `GRC_RPC_TIMEOUT=30` bought 30s of patience about
whether the daemon EXISTS on top of 30s of patience about whether it has
ANSWERED. Those are different questions and only one of them is worth 30s.

A local daemon completes a handshake in well under a millisecond, and a remote
endpoint over the agent proxy in one or two seconds. Neither takes thirty. What
takes thirty -- what the read leg is FOR -- is a daemon that accepted the
connection and is now rescanning its wallet, or an RPC that is genuinely slow.
That leg is untouched here.

MEASURED, AND THE TWO CASES ARE FOUR ORDERS OF MAGNITUDE APART. A port that
REFUSES answers in 0.0015s regardless of the timeout, because the kernel sends
RST and `requests` raises immediately. A port that DROPS -- no listener behind a
firewall that discards, or Docker Desktop's host.docker.internal gateway with
nothing on the far side -- cannot be distinguished from a slow one except by
waiting, and the waiting is what the operator's 65s cycle was made of. This
module is about the dropping case; the refusing case was never the problem and
is not changed.

THE SAFETY ARGUMENT, AND IT IS WHY THIS IS A FIX RATHER THAN A SPEED TRADE.

CLAUDE.md rule 16 sends anything changing "whether a swap may proceed at all"
back to the operator, and an RPC timeout on the payout path is squarely that. So
the case has to be made rather than assumed, and it rests on a distinction the
two legs do not share:

  a CONNECT timeout on a send is UNAMBIGUOUS. If the handshake never completed,
  no bytes of the request reached the daemon, so NOTHING WAS SUBMITTED. The
  caller may retry without any risk of a double send.

  a READ timeout on a send is AMBIGUOUS, and this tree already says so in two
  places -- chains/icp.py:1360 ("a retry after a timeout DOUBLE-PAYS") and
  chains/xrp.py:1832 ("a timeout here does NOT mean nothing was submitted").
  The request was delivered; only the answer is missing.

Shortening the scalar would have shortened BOTH, which would have traded a
double-pay risk for speed. Splitting them shortens ONLY the leg where a timeout
proves nothing happened, and leaves the leg where it proves nothing at all
exactly as long as the operator configured it. The worst case the stop budget is
sized against is therefore unchanged: see stop_budget.LONGEST_RPC_TIMEOUT_SECONDS,
which reads the READ leg and still computes a 65s worker grace, because a live
daemon can still spend every second of ICP_CALL_TIMEOUT.

WHAT THIS DOES NOT COVER. chains/icp.py does not post HTTP -- it runs `dfx` as a
subprocess with a single `timeout=`, which has no connect leg to split and is
left alone. swap_terminal/transactions.py posts with its own
RPC_TIMEOUT_SECONDS = 10 and is NOT changed: it is a read-only Tkinter GUI that
nothing in this tree imports (its own header establishes both), so it is off the
money path and off the worker cycle this module was written for. That is named
work, not a baseline (rule 19): a GUI that hangs 10s per batch against a dead
daemon is a real annoyance and a separate change.
"""

from __future__ import annotations

import os

#: SECONDS allowed for the TCP (and, for an https endpoint, TLS) handshake alone.
#:
#: Named in seconds, not microfortnights, because it is handed straight to
#: `requests(timeout=)` -- CLAUDE.md rule 6 is explicit that seconds stay where an
#: external interface demands them, and that the conversion happens at the print.
#:
#: 5.0 rather than something tighter, and the number is a MARGIN over two measured
#: handshakes rather than a guess at a round figure:
#:
#:   a local daemon over Docker's host.docker.internal gateway   sub-millisecond
#:   a remote endpoint (Solana devnet, an XRP testnet node)      1-2s, through the
#:                                                              agent proxy's TLS
#:
#: So 5.0 is roughly 5000x the first case and ~3x the second, while still being 6x
#: shorter than the 30s it replaces. The thing it must not do is start FAILING a
#: handshake that would have succeeded, because that would turn a slow network into
#: an unreachable chain -- which on the deposit path means a deposit reported absent.
#: A tighter value buys 2s of stop budget and risks exactly that.
#:
#: NOT ST_RPC_CONNECT_TIMEOUT_SECONDS, and the first draft was. In this tree `_RPC_`
#: means PER CHAIN -- BTC_RPC_TIMEOUT, GRC_RPC_PASSWORD, LTC_RPC_WALLET -- while `ST_`
#: means terminal-wide. A name carrying both reads, in a sorted .env.example, as if
#: there were a chain called ST. That is rule 11's defect exactly: one vocabulary,
#: derived in one place, and a marker that resolves to the wrong thing is silent.
#:
#: It was caught by a gate rather than by review --
#: tests/test_compose_chain_variables.test_every_chain_shaped_variable_compose_passes_is_one_config_reads
#: matches `_RPC_` and demands config.py read it, and config.py does not read this
#: one (this module does). The fix was the NAME, not an exception in the gate: rule 19
#: is explicit that a suppression which stops a symptom being reported is not a fix.
#:
#: ASCII name, no µ, per rule 6's interface boundary: an operator has to type this
#: into a shell.
CONNECT_TIMEOUT_SECONDS = 5.0


def _connect_seconds_from_env(default: float = CONNECT_TIMEOUT_SECONDS) -> float:
    """ST_CHAIN_CONNECT_TIMEOUT_SECONDS as a float, where UNSET and EMPTY both mean `default`.

    EMPTY COUNTS AS UNSET for the reason stop_budget._seconds_from_env() records at
    length and which cost a whole shipped-but-inert fix on 2026-10-11: a compose
    `environment:` entry ALWAYS sets the variable, so if this module treated "" as a
    value then a forwarded `"${ST_CHAIN_CONNECT_TIMEOUT_SECONDS:-}"` would mean zero
    seconds -- every handshake failing instantly, every chain unreachable, on a
    deployment where the operator set nothing at all.

    A NON-NUMBER RAISES rather than falling back to the default, for the same reason
    stop_budget does: `ST_CHAIN_CONNECT_TIMEOUT_SECONDS=5s` silently meaning 5.0 is a
    setting an operator believes they changed. The worker refuses to start and says
    which variable, which is a defect they can see.
    """
    raw = os.getenv("ST_CHAIN_CONNECT_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"ST_CHAIN_CONNECT_TIMEOUT_SECONDS={raw!r} is not a number of seconds. "
            "It bounds the TCP handshake to a chain RPC endpoint and nothing else; "
            f"leave it unset for the default of {default}s."
        ) from exc
    if value <= 0:
        raise ValueError(
            f"ST_CHAIN_CONNECT_TIMEOUT_SECONDS={raw!r} is not a positive number of seconds. "
            "Zero or negative would fail every handshake instantly and report every "
            "chain unreachable -- which on the deposit path means a real deposit "
            f"reported absent. Leave it unset for the default of {default}s."
        )
    return value


def connect_and_read(read_seconds: float, connect_seconds: float | None = None) -> tuple[float, float]:
    """The `(connect, read)` pair to hand `requests(timeout=)` for a configured RPC timeout.

    `read_seconds` is the adapter's own configured timeout -- `<ASSET>_RPC_TIMEOUT`,
    which an operator sets per chain. It is returned UNCHANGED as the read leg. This
    function only ever decides the connect leg.

    THE CONNECT LEG IS CAPPED AT THE READ LEG, and that `min` is the one piece of
    logic here rather than an edge case. An operator who sets `GRC_RPC_TIMEOUT=1`
    has said "I want to wait at most one second for Gridcoin." Returning (5.0, 1.0)
    would have RAISED their total worst case from 1s to 6s -- a lower setting making
    the thing slower, which is the kind of inversion nobody checks for because
    nobody expects it. So the connect leg is never longer than the whole budget the
    operator allowed.

    `connect_seconds=None` means read the environment, which is the call every
    adapter makes. An explicit number is for tests, which must not depend on the
    ambient environment of the machine running them.

    Returns the pair in `requests`' own order: (connect, read). Getting that
    backwards would silently swap the two legs and is exactly the kind of thing
    rule 17 says to test rather than read, so tests/test_rpc_connect_timeout.py
    asserts on the pair's ORDER against a real refused port.
    """
    resolved_connect = _connect_seconds_from_env() if connect_seconds is None else float(connect_seconds)
    read = float(read_seconds)
    return (min(resolved_connect, read), read)
