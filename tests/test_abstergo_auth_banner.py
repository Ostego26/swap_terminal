"""The Express server's startup banner must name the routes it actually guards.

Role: code hygiene (read-only)
Reads: swap_terminal/grc-sol-swap/abstergo_exchange/server.js and auth.js, as text
Writes: nothing
Can move funds: no
Mainnet-safe: yes

A CLEAN GATE, not a baseline (CLAUDE.md rule 19).

WHY THIS EXISTS. server.js:788 prints, on every start:

    Routes requiring the shared secret: GET /swap-intents/:id,
    POST /swap-intents/:id/verify-gridcoin, POST /swap-intents/:id/execute

That line is a HAND-WRITTEN STRING. Nothing derives it from the routes that
actually call requireSharedSecret(), so adding a guard, removing one, or adding
a new money-moving route leaves the banner saying what used to be true. Rule 14
is about output that says nothing during a wait; this is the worse version --
output that says the wrong thing confidently, about which routes are
authenticated, on a server that signs Solana transfers.

IT HAS ALREADY HAPPENED ONCE IN THE PROSE. auth.js carried a route table
showing POST /swap-intents/:id/execute as unauthenticated, followed by a
present-tense paragraph describing it as an open hole that signs SOL. Measured
2026-10-05: it is guarded (server.js:690) and has been for some time. A reader
working from that file concluded an unauthenticated route could move funds and
repeated it. The table is now labeled BEFORE and AFTER; this test is what keeps
the AFTER honest, because a label is a promise and a promise needs a check.

PARSED FROM TEXT, AND THAT IS THE RIGHT TRADE HERE. This is a Python suite
reading a JavaScript file, which is normally a bad idea -- but the alternative
is asserting on the SQL-text equivalent of nothing: the repository's own
behavioral-verification principle says verify by outcome, and the outcome here
IS a string the server prints. Running the server to read its banner would need
node, five environment variables and a port; reading which handlers contain the
call is the same question asked statically. If server.js is ever restructured so
this parse breaks, the test fails loudly rather than passing vacuously -- see
test_the_route_parse_found_something.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ABSTERGO = REPO_ROOT / "swap_terminal" / "grc-sol-swap" / "abstergo_exchange"
SERVER_JS = ABSTERGO / "server.js"
AUTH_JS = ABSTERGO / "auth.js"

_ROUTE = re.compile(r"app\.(get|post|put|delete|patch)\('([^']+)'")
_GUARD = re.compile(r"^\s*requireSharedSecret\(req\);")

#: The banner line, matched by its stable prefix rather than its whole text, so a
#: reworded sentence does not fail this for the wrong reason.
_BANNER_PREFIX = "Routes requiring the shared secret:"


def routes() -> list[tuple[str, str, bool]]:
    """(method, path, guarded) for every route in server.js, in declaration order.

    A route's handler is taken to run from its own `app.<method>(` line to the
    next one. That is crude and it is sufficient: Express handlers here are
    declared one after another at the top level, and the alternative is parsing
    JavaScript, which would be a second implementation of somebody else's
    grammar (rule 8).
    """
    lines = SERVER_JS.read_text().splitlines()
    declarations = [(i, m) for i, line in enumerate(lines) if (m := _ROUTE.search(line))]
    guard_lines = [i for i, line in enumerate(lines) if _GUARD.match(line)]
    found = []
    for index, (start, match) in enumerate(declarations):
        end = declarations[index + 1][0] if index + 1 < len(declarations) else len(lines)
        guarded = any(start < g < end for g in guard_lines)
        found.append((match.group(1).upper(), match.group(2), guarded))
    return found


def banner_line() -> str:
    """The banner's STRING CONTENT, not the source line that prints it.

    The source line is `console.log('Routes requiring ... /execute');`, so a
    naive read leaves `');` glued to the last path and the route comparison then
    fails on a route that IS guarded -- which is a gate failing for a reason that
    has nothing to do with what it guards. Found by running it.
    """
    for line in SERVER_JS.read_text().splitlines():
        if _BANNER_PREFIX not in line:
            continue
        first = line.find("'")
        last = line.rfind("'")
        if first != -1 and last > first:
            return line[first + 1:last]
        return line
    return ""


def test_the_route_parse_found_something():
    """Otherwise every assertion below passes by examining nothing (rule 17)."""
    parsed = routes()
    assert len(parsed) >= 5, (
        f"only {len(parsed)} routes parsed out of {SERVER_JS}. Either the file was "
        f"restructured or the app.<method>('...') pattern no longer matches, and this "
        f"gate is now checking nothing rather than passing."
    )
    assert any(guarded for _, _, guarded in parsed), (
        "no route was found to call requireSharedSecret(req), which would mean either "
        "every route is open or the guard pattern stopped matching"
    )


def test_the_startup_banner_names_every_guarded_route():
    """The printed list must not omit a route that IS guarded.

    An omission here understates the protection, which is the less dangerous
    direction and still wrong: an operator reading the banner would think a
    guarded route is open and might add a second guard, or worse, route around it.
    """
    line = banner_line()
    assert line, f"no line containing {_BANNER_PREFIX!r} in {SERVER_JS}"
    missing = [
        f"{method} {path}"
        for method, path, guarded in routes()
        if guarded and path.replace(":intentId", ":id") not in line
    ]
    assert not missing, (
        "these routes call requireSharedSecret() but the startup banner does not "
        "name them:\n  " + "\n  ".join(missing)
        + f"\n\nbanner: {line.strip()}"
    )


def test_the_startup_banner_claims_no_route_it_does_not_guard():
    """THE DANGEROUS DIRECTION. The banner must not name a route that is open.

    A banner claiming a money-moving route is authenticated when it is not is the
    exact shape rule 13 names -- output that reads as success while nothing is
    happening -- applied to an auth boundary. Every path the banner lists is
    checked back against the parse.
    """
    line = banner_line()
    assert line, f"no line containing {_BANNER_PREFIX!r} in {SERVER_JS}"
    claimed = re.findall(r"(GET|POST|PUT|DELETE|PATCH)\s+(/\S*?)(?:,|$)", line.split(_BANNER_PREFIX, 1)[1])
    guarded = {
        (method, path.replace(":intentId", ":id"))
        for method, path, is_guarded in routes()
        if is_guarded
    }
    overclaimed = [
        f"{method} {path}" for method, path in claimed
        if (method, path.rstrip(",")) not in guarded
    ]
    assert not overclaimed, (
        "the startup banner claims these routes require the shared secret, and no "
        "requireSharedSecret(req) call was found in their handlers:\n  "
        + "\n  ".join(overclaimed)
        + f"\n\nguarded per the parse: {sorted(guarded)}"
    )


def test_auth_js_route_table_is_labeled_as_historical():
    """The BEFORE table must say it is BEFORE, because it says /execute is open.

    It is kept rather than overwritten -- the drift is the point (rule 1) -- but a
    superseded measurement that does not announce itself is indistinguishable from
    a current one, and this one is about whether a route that signs SOL transfers
    is authenticated.
    """
    text = AUTH_JS.read_text()
    assert "BEFORE --" in text, (
        "auth.js's route table shows POST /swap-intents/:id/execute as unauthenticated. "
        "That was true once and is not now (server.js:690). The table may stay, but it "
        "must be labeled 'BEFORE --' so a reader cannot take it for the current state."
    )
    assert "AFTER -- MEASURED" in text, (
        "auth.js documents the old auth boundary and not the current one. A file that "
        "states only the superseded state is how a reader concludes an unauthenticated "
        "route signs SOL transfers -- which happened on 2026-10-05."
    )
