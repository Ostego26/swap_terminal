"""Whether two failures are the SAME failure, for a log line that promises to speak once.

Role: submodule (one decision, and a pure function)
Reads: nothing
Writes: nothing
Can move funds: no -- it decides whether a WARNING is printed. Nothing downstream
       of it gates a payout, which is checked rather than assumed: its only
       caller, payout_service.refresh_wallet_inventory(), continues to the next
       asset either way and the row keeps its previous values regardless.
Mainnet-safe: yes

WHY THIS EXISTS, measured on the operator's host 2026-10-11 from twelve consecutive
payout cycles.

services/payout_service.py says this, and said it from the day it was written:

    wallet inventory for LTC NOT refreshed (previous values kept), and this is said
    ONCE per process rather than every cycle: ...

It was said EVERY CYCLE. Eleven times for LTC and eleven for GRC across cycles 69 to
80, roughly fourteen minutes, each about 500 bytes of identical text. A log line
describing its own cadence, wrong about it, in a file whose rule 14 says silence is a
defect and that a log which cries wolf is a log nobody reads the day something real
happens.

THE CAUSE, AND IT IS TWO DEFECTS IN ONE 27-CHARACTER EXPRESSION. The key was
`(asset, str(exc)[:200])`, and `str()` of a requests ConnectTimeout reads:

    HTTPConnectionPool(host='host.docker.internal', port=19332): Max retries exceeded
    with url: / (Caused by ConnectTimeoutError(<HTTPConnection(host=
    'host.docker.internal', port=19332) at 0x7caa492a5340>, 'Connection to
    host.docker.internal timed out. (connect timeout=30.0)'))

  the CPython object address is at index 182, so it is INSIDE the slice. A fresh
  HTTPConnection is built per attempt, so the address is new every cycle, the
  signature is new every cycle, and the set never matches itself. Measured
  addresses from six consecutive LTC cycles: 0x7caa492a5340, 0x7caa492a3ce0,
  0x7caa494b98b0, 0x7caa498dd790, 0x7caa492a3380, 0x7caa492a4bc0.

  the message is 274 characters, so `[:200]` CUT THE REASON. 'Connection to
  host.docker.internal timed out. (connect timeout=30.0)' begins at index 205.
  The window kept the only part that changes and discarded the only part that
  identifies the failure -- the exact inverse of the intent stated two lines
  above the set, which is "keyed on the asset AND the message, so a DIFFERENT
  failure for the same asset -- a daemon that was up and is now down -- still
  reports." Under the old key a daemon going from refused to timing out, or from
  timing out to a 401, produced a signature that differed only in a hex address
  it would have differed in anyway.

So the two defects cancelled into something that LOOKED like it worked: the warning
appeared, which is what an operator notices, and it appeared too often, which is
what nobody notices until they read fourteen minutes of log at once.

WHAT IS NORMALIZED, and nothing else. Hex object addresses, because they are the
measured cause and because they carry no information a reader or a key can use: two
failures that differ only by where Python put an object are the same failure. The
host, the port, the URL, the timeout value and the exception type are all KEPT,
because each of them changing IS a different failure and the suppression must let it
through.

WHY NOT KEY ON THE EXCEPTION TYPE ALONE, which would also have fixed the repetition.
Because it would have reintroduced the bug the message was there to prevent from the
other side: ConnectTimeout against port 19332 and ConnectTimeout against port 25715
are one key, so whichever chain failed second would be silent forever. The type is
added to the key rather than replacing the message.
"""

from __future__ import annotations

import re

#: `at 0x7caa492a5340` -> `at 0xADDR`. Matched with the `at ` prefix and a word
#: boundary rather than as a bare `0x[0-9a-f]+`, so a hex value that is part of the
#: MESSAGE survives. That distinction is not hypothetical in this tree: a Bitcoin-style
#: daemon's RPC error can carry a txid, and chains/icp.py reports account identifiers as
#: hex, both of which identify a failure and must not be erased.
_OBJECT_ADDRESS = re.compile(r"\bat 0x[0-9a-fA-F]+\b")

#: How much of the normalized message the key keeps.
#:
#: 400 and not 200, and the number is measured rather than round. The message that
#: exposed this is 274 characters and its discriminating tail -- 'Connection to
#: host.docker.internal timed out. (connect timeout=30.0)' -- starts at 205, so the old
#: 200 cut precisely the part that says what went wrong. 400 holds that message whole
#: with 126 characters of margin.
#:
#: A CAP AT ALL, rather than the whole string, because the set lives for the life of
#: the process and an exception message is not bounded by anything this tree controls:
#: a daemon that returns an HTML error page inside an RPC error would put kilobytes in
#: it. Bounded growth per distinct failure matters more than the tail of a message no
#: operator reads past the first line of.
MESSAGE_KEY_LENGTH = 400


def normalize_failure_text(text: str) -> str:
    """`text` with volatile object addresses replaced, so repeats of one failure match.

    Pure, and the only transformation is the address. See this module's header for why
    the host, port, URL and timeout value are deliberately left alone.
    """
    return _OBJECT_ADDRESS.sub("at 0xADDR", text)


def failure_signature(subject: str, exc: BaseException) -> tuple[str, str, str]:
    """The key under which `exc` for `subject` counts as "already reported".

    `subject` is whatever the suppression is per -- an asset, for the one caller today.

    THREE PARTS, and each earns its place:

      subject   so GRC failing does not silence LTC.
      type      so a daemon that stops refusing and starts timing out reports again,
                even if its message truncates to the same thing.
      message   normalized and capped, so the SAME failure repeating is suppressed
                while a different one for the same subject is not.

    Returning a tuple rather than a hashed string keeps the key readable in a debugger
    and in a test failure, which is where anyone looking at this will be.
    """
    normalized = normalize_failure_text(str(exc))
    return (subject, type(exc).__name__, normalized[:MESSAGE_KEY_LENGTH])
