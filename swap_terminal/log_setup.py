"""Install a log handler, so that logger.info() reaches somebody.

Role: module (process setup; holds no decision about swaps, amounts or funds)
Reads: nothing
Writes: nothing persistent. It mutates the ROOT LOGGER of the calling process,
        which is a global -- which is why it is a function an entry point CALLS
        rather than something that happens on import. Three modules in this tree
        already configure logging at import time (transactions.py at DEBUG,
        identity.py, orderbook_grc.py) and that is rule 12's named defect: an
        import-time side effect makes every later import order-dependent.
Can move funds: no
Mainnet-safe: yes

A top-level module rather than a function in workers/common.py, because app.py
needs it too and a Flask app importing from workers/ would say the web server is
a kind of worker. Both are entry points; this is a setup step they share.

INFO, AND NEVER DEBUG. Measured 2026-09-26 by reading the root logger after
importing each of the three workers:

    handlers=[] level=WARNING isEnabledFor(INFO)=False

Nothing in the serving or worker path called basicConfig. The three modules that
do it at import are imported only by tests -- grepped, and confirmed by the
reading above, which found level WARNING rather than the DEBUG transactions.py
would have installed. So all 56 logger.info() calls in this application were
discarded, and logger.warning/error reached logging.lastResort unformatted: no
timestamp, no logger name, on workers whose whole job is to be watched.

THE CONCRETE COST, and why this was fixed rather than noted.
chains/gridcoin_wallet_lock.py logs the LOCK -> full UNLOCK -> LOCK -> staking
sequence at INFO, one line per step. That sequence is the one the operator
specified, and seeing it happen against a real daemon is the whole point of the
first live payout. Its test passes -- pytest's caplog installs its own handler --
so the calls look present to anyone who greps for them and reached nothing in
production. A test asserting on a log that never leaves the process is the exact
shape of a check that proves nothing.

DEBUG IS NOT AN OPTION, for a specific reason rather than tidiness:
modules/atomic_htlc_scripts.py:417 logs secret-hash material at DEBUG, verified
as LIVE code by parsing the file rather than grepping it. A secret hash is public
in an HTLC, so this is not a preimage leak -- but a default that writes
fund-bearing material to a log file is the operator's decision (rule 16), not a
side effect of fixing a silence. 65 DEBUG call sites would also bury the 56 INFO
ones.

An audit flagged modules/utils.py:38 as logging a raw preimage at DEBUG. IT DOES
NOT: line 38 is inside the module docstring, quoting the line that was removed on
2026-09-24. An ast parse of that file reports live debug calls at 91, 116 and 139,
and all three log a hash. Recorded here because the next reader will grep the same
file and find the same false positive -- which is the cost of rule 1's verbose
docstrings, and cheaper than not having them.
"""

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """Install a root handler at INFO, so logger.info() reaches the process's log.

    basicConfig() and NOT force=True: if something upstream has already installed a
    handler, replacing it would be this function deciding where another component's
    output goes. basicConfig is a no-op when handlers exist, which is the right
    default for a step an entry point calls at startup.

    WHERE THE OUTPUT GOES. supervisor.py:253-259 opens runtime/<worker>.log and
    passes it as the child's stdout with stderr=STDOUT, and basicConfig's
    StreamHandler writes to stderr -- so under the supervisor these lines land in
    the same file as the print() banner, interleaved in real time. Run directly in
    a terminal, which is how the operator runs them, they go to the terminal.
    """
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
