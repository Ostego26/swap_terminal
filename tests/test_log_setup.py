"""That logger.info() actually reaches somebody.

Role: test (subprocesses and an isolated root logger; opens no socket, touches no
      wallet)
Reads: log_setup.py, workers/common.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE HEADLINE TESTS RUN IN A SUBPROCESS, AND THAT IS THE WHOLE POINT.

pytest's own logging plugin installs handlers on the root logger, and
logging.basicConfig() is a no-op when handlers already exist. So a test that calls
configure_logging() in-process and then asserts "the root logger has a handler"
passes whether or not configure_logging() does anything at all -- it would be
asserting pytest's setup.

That is the same defect these tests exist to close, one level up:
chains/gridcoin_wallet_lock.py's test asserts on caplog, caplog installs its own
handler, and the assertion passed for as long as those log lines reached nothing in
production (measured 2026-09-26: root logger handlers=[] level=WARNING after
importing any of the three workers).

So the real behavior is checked the way the worker actually runs it -- a fresh
interpreter, no plugins -- and the in-process tests below use a fixture that
strips the root logger first and puts it back afterwards.
"""

import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "swap_terminal"
sys.path.insert(0, str(APP_ROOT))

def in_fresh_interpreter(body: str) -> subprocess.CompletedProcess:
    """Run `body` in a new python, with swap_terminal/ importable. Returns the result."""
    return subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(APP_ROOT)!r})\n{body}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# --- what a worker's process actually does ------------------------------------

def test_without_configure_logging_an_info_line_reaches_nothing():
    """The state every worker started in until 2026-09-26. Asserted so that the
    test below is measuring a change rather than describing the status quo."""
    result = in_fresh_interpreter(
        "import logging\n"
        "logging.getLogger('chains.gridcoin_wallet_lock').info('SENTINEL-INFO')\n"
        "print('root handlers:', logging.getLogger().handlers)\n"
    )

    assert "SENTINEL-INFO" not in result.stderr, "an INFO line escaped with no handler installed"
    assert "SENTINEL-INFO" not in result.stdout
    assert "root handlers: []" in result.stdout


def test_configure_logging_lets_an_info_line_out():
    result = in_fresh_interpreter(
        "from log_setup import configure_logging\n"
        "configure_logging()\n"
        "import logging\n"
        "logging.getLogger('chains.gridcoin_wallet_lock').info('SENTINEL-INFO')\n"
    )

    assert "SENTINEL-INFO" in result.stderr, f"INFO still discarded. stderr={result.stderr!r}"
    # The formatter is part of the fix, not decoration: an unformatted line through
    # logging.lastResort has no timestamp and no logger name, which on a worker that
    # cycles every 15 seconds is the difference between a log and a pile of strings.
    assert "INFO" in result.stderr
    assert "chains.gridcoin_wallet_lock" in result.stderr


def test_configure_logging_does_not_turn_on_debug():
    """THE ONE THAT PROTECTS A SECRET.

    modules/atomic_htlc_scripts.py:417 logs secret-hash material at DEBUG (live
    code -- verified by parsing the module, not grepping it). A hash is public in an
    HTLC so this is not a preimage leak, but a default that writes fund-bearing
    material into runtime/<worker>.log is the operator's decision, not something a
    fix for a silence gets to make.

    MUTATION: change level=logging.INFO to logging.DEBUG in log_setup.py. This is
    the only test that fails.
    """
    result = in_fresh_interpreter(
        "from log_setup import configure_logging\n"
        "configure_logging()\n"
        "import logging\n"
        "logging.getLogger('modules.atomic_htlc_scripts').debug('SENTINEL-DEBUG')\n"
        "logging.getLogger('modules.atomic_htlc_scripts').info('SENTINEL-INFO')\n"
    )

    assert "SENTINEL-INFO" in result.stderr, "INFO must be on, or the test proves nothing"
    assert "SENTINEL-DEBUG" not in result.stderr, "DEBUG would write secret-hash material to the log"


def test_a_worker_gets_it_from_announce_start_without_asking():
    """All three workers call announce_start() first. None calls configure_logging().

    That is deliberate -- one startup step with three call sites is three chances for
    a new worker to omit it, and the omission is invisible. Asserted through the real
    function in a real interpreter, because "announce_start calls it" read off the
    source is not the same as "a worker's INFO lines come out".
    """
    result = in_fresh_interpreter(
        "from workers.common import announce_start\n"
        "announce_start('test_worker', 15.0, 4242)\n"
        "import logging\n"
        "logging.getLogger('services.payout_service').info('SENTINEL-AFTER-BANNER')\n"
    )

    assert "test_worker: starting" in result.stdout, f"banner missing. stdout={result.stdout[:400]!r}"
    assert "SENTINEL-AFTER-BANNER" in result.stderr, "announce_start() did not install the handler"


# --- the narrower claims, also in a subprocess --------------------------------
#
# AN EARLIER DRAFT TESTED THESE IN-PROCESS, with a fixture that stripped the root
# logger's handlers and restored them afterwards. Both tests failed, and they
# failed correctly: pytest's logging plugin re-attaches its LogCaptureHandler to
# the root logger per test, AFTER fixture setup, so the fixture's "bare" root
# logger was not bare by the time configure_logging() ran. The assertions were
# about pytest's handler, not ours.
#
# Which is this file's own docstring happening to its own tests, and the reason
# they were deleted rather than worked around with -p no:logging or a monkeypatched
# plugin (rule 19: fix the cause, do not quiet the finding). A fresh interpreter has
# no plugins and is what a worker actually starts.


def test_it_does_not_replace_a_handler_somebody_else_installed():
    """basicConfig() without force=True, and the omission is the decision.

    Replacing an existing handler would be this function choosing where another
    component's output goes -- gunicorn installs its own, and so does pytest.

    MUTATION: add force=True in log_setup.py. The count becomes 1 and the marker is
    gone.
    """
    result = in_fresh_interpreter(
        "import logging\n"
        "first = logging.StreamHandler()\n"
        "first.set_name('installed-by-somebody-else')\n"
        "logging.getLogger().addHandler(first)\n"
        "from log_setup import configure_logging\n"
        "configure_logging()\n"
        "root = logging.getLogger()\n"
        "print('handler count:', len(root.handlers))\n"
        "print('names:', [h.get_name() for h in root.handlers])\n"
    )

    assert "handler count: 1" in result.stdout, (
        f"a second handler would double every line. stdout={result.stdout!r}"
    )
    assert "installed-by-somebody-else" in result.stdout, "the existing handler was displaced"


def test_the_level_is_info_exactly():
    """Stated as a number rather than inferred from which lines came out, so that a
    future change to WARNING or DEBUG fails here and says which."""
    result = in_fresh_interpreter(
        "from log_setup import configure_logging\n"
        "configure_logging()\n"
        "import logging\n"
        "print('level:', logging.getLevelName(logging.getLogger().level))\n"
    )

    assert "level: INFO" in result.stdout, f"stdout={result.stdout!r}"
