"""No log line may carry an HTLC preimage (the chain-safety rules).

Role: test (read-only)
Reads: swap_terminal/modules/utils.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

"Never reveal a preimage. Not in a log, not in a commit, not in a pasted
diagnostic, not in an error message. Log `secret_hash`, never `secret`."

Before 2026-09-24 generate_secret() ended with

    logger.debug(f"Generated secret: {secret.hex()}")

and modules/utils.py configured its own logger with setLevel(DEBUG) and a
StreamHandler on stdout AT IMPORT, so every secret the function ever produced
was printed in full, by default, with no application opt-in.

These tests capture the real log records the real function emits at the most
verbose level and assert the preimage is not among them. Reading the source for
the absence of a format string would not survive the next refactor; this does.
"""

import hashlib
import logging

from modules.utils import generate_secret, sha256_hash


def _all_log_text(caplog) -> str:
    """Every rendered record plus every raw argument, as one string.

    Both halves matter. `logger.debug("x=%s", value)` leaves `value` in
    record.args whether or not the message is ever formatted, so a test that
    only reads record.message could miss a leak that a handler would print.
    """
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
    return " ".join(parts)


def test_generate_secret_does_not_log_the_preimage(caplog):
    with caplog.at_level(logging.DEBUG, logger="modules.utils"):
        secret = generate_secret(32)

    text = _all_log_text(caplog)
    assert secret.hex() not in text, "the HTLC preimage appeared in a log record"
    # Nor in any other encoding a helpful refactor might reach for.
    assert str(secret) not in text
    assert secret.hex().upper() not in text


def test_generate_secret_logs_the_hash_instead(caplog):
    """The replacement has to be USEFUL, or the next person puts it back.

    The SHA-256 hash is what goes into the redeem script and is public the
    moment the contract is funded, so it names the swap a log line is about
    without being the value that spends it.
    """
    with caplog.at_level(logging.DEBUG, logger="modules.utils"):
        secret = generate_secret(32)

    text = _all_log_text(caplog)
    assert hashlib.sha256(secret).hexdigest() in text
    assert "32" in text  # the length, so a truncated secret is visible


def test_sha256_hash_logs_its_output_but_never_its_input(caplog):
    """The hash of a preimage is safe to log. The preimage passed IN is not."""
    preimage = bytes.fromhex("ab" * 32)
    with caplog.at_level(logging.DEBUG, logger="modules.utils"):
        digest = sha256_hash(preimage)

    text = _all_log_text(caplog)
    assert digest.hex() in text
    assert preimage.hex() not in text


def test_the_module_attaches_no_handler_of_its_own():
    """The delivery mechanism, removed with the leak.

    A library module that calls setLevel(DEBUG) and attaches a StreamHandler at
    import decides logging policy for every program that imports it, and there
    is no way for the application to turn it back off short of reaching into
    the logger object. That is what made the leak above reach a terminal by
    default rather than only in a debugging session.
    """
    logger = logging.getLogger("modules.utils")
    assert logger.handlers == []
    assert logger.level == logging.NOTSET
