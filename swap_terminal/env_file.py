"""Read `.env` into the process environment. For ENTRY POINTS only, never on import.

Role: submodule -> function (assignments_in is the parse; load_into_environ is
      the one side effect, and only an entry point may call it)
Reads: .env at the repository root
Writes: os.environ, and ONLY names that are not already set
Can move funds: no. It reads no chain and opens no socket. It DOES decide which
      daemon the caller will later address, which is why the precedence below is
      stated rather than assumed.
Mainnet-safe: yes to import -- importing this module does nothing at all, which
      is the entire design (see below).

=============================================================================
WHY THIS EXISTS, measured on the operator's host 2026-10-12.
=============================================================================

`swap_stack.py off` had just learned to stop the chain daemons, after the operator
asked three times. Their first run printed:

    3. chain daemons -- the operator asked for this twice
       BTC  NOT TOUCHED  BTC_RPC_PORT is unset, so this process builds no BTC adapter
       GRC  NOT TOUCHED  GRC_RPC_PORT is unset, so this process builds no GRC adapter
       LTC  NOT TOUCHED  LTC_RPC_PORT is unset, so this process builds no LTC adapter

THOSE PORTS WERE NOT UNSET. The container was talking to 19332 and 25715 in the
same minute. They were unset IN THE SHELL, because `docker compose` reads `.env`
by itself and a bare `python3 swap_stack.py` does not -- so the feature shipped,
went green, and did nothing. Third time in one session that something passed its
tests and was inert on the host.

AND THE DEFECT WAS ALREADY WRITTEN DOWN, in assets/swap-terminal.desktop, from
2026-10-09: "they clicked 'Swap Terminal' and got a host gunicorn on :5000
reporting all five chains NOT CONFIGURED, because nothing in the serving path
reads a .env ... `docker compose` reads .env by itself, so the CONTAINERIZED stack
gets the chain ports and this one does not."

=============================================================================
WHY config.py MUST NOT DO THIS ITSELF
=============================================================================

config.py's own header refuses it, and the refusal is right: "adding
load_dotenv() to a module read at import is the import-time side effect rule 12
names as a measured past defect." A module that mutates os.environ when imported
makes every later import order-dependent, and this tree has ~90 modules that
import config.

So the load belongs to an ENTRY POINT -- a file an operator names on a command
line, which has exactly one import order and controls it. That is why this module
does nothing on import and why load_into_environ() has to be CALLED. The cost is
that each entry point must call it before importing config, which is an ordering
constraint a reader cannot see; swap_stack.py's call site says so in capitals, and
tests/test_env_file.py asserts the ordering over the source rather than trusting
the comment.

=============================================================================
PRECEDENCE: THE SHELL WINS, WHICH IS WHAT COMPOSE DOES
=============================================================================

`setdefault`, never assignment. docker compose gives a variable already in the
shell environment precedence over the same name in `.env`, so an operator who
exports GRC_RPC_PORT for one command gets that value in the container -- and must
get the same value here, or the two halves of one terminal would address different
daemons. With three Gridcoin wallets on that host (OPEN_FINDINGS finding 2) the
cost of disagreeing is `off` stopping a daemon the container never used.

NOTHING IS EVER PRINTED FROM THE FILE. `.env` holds rpcpassword, SECRET_KEY and
payout seeds. load_into_environ() returns the NAMES it set and never the values,
and the one caller prints a count. CLAUDE.md: "Never print a secret."
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: The repository root, from this file's own location. Two parents: this module
#: lives in swap_terminal/, and .env is beside swap_stack.py at the root.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

#: An uncommented `NAME=value` line. The same shape configure_env.existing_assignments()
#: uses, and rule 8 requires both sites to name the other: that function reads `.env`
#: to avoid re-asking for what is already set, this one reads it to populate the
#: environment. They are kept separate rather than merged because configure_env is an
#: operator-facing writer that must not import a module whose whole purpose is a side
#: effect on os.environ -- and because a shared parser would be a third thing to keep
#: in step for two callers whose needs have not diverged in any way yet. If a third
#: caller appears, that is the signal to merge them.
_ASSIGNMENT = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")

#: The shortest value that can be QUOTED: an opening and a closing mark. Named
#: because `len(value) >= 2` reads as arithmetic where this reads as the rule, and
#: because a bare 2 beside a slice is where an off-by-one lives.
QUOTED_MINIMUM_LENGTH = 2


def assignments_in(text: str) -> dict[str, str]:
    """Every uncommented assignment in `.env`'s text. PURE -- takes text, not a path.

    A STRING AND NOT A FILENAME, so a test can seed a file's content without writing
    one and without a temporary directory (rule 10: the decision is the smallest
    testable piece). load_into_environ() is the thin part that reads the disk.

    QUOTES ARE STRIPPED because compose strips them: `GRC_RPC_PORT="25779"` in a
    `.env` reaches a container as 25779, not as a quoted string, and a terminal whose
    two halves disagreed on that would build an adapter against port `"25779"` and
    fail at int() with a message about a literal.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        match = _ASSIGNMENT.match(stripped)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= QUOTED_MINIMUM_LENGTH and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[match.group(1)] = value
    return found


def load_into_environ(path: Path | None = None) -> list[str]:
    """Set every name in `.env` that is NOT already set. Returns the NAMES it set.

    THE NAMES AND NEVER THE VALUES. `.env` holds rpcpassword, SECRET_KEY and payout
    seeds, and a return value that carried them would reach whatever the caller
    prints.

    A MISSING `.env` IS NOT AN ERROR. A host deployment may export everything from a
    shell profile instead, and a fresh checkout has no `.env` at all until
    configure_env.py writes one. Returning [] lets the caller say "nothing was
    loaded", which reads differently from "the file was empty" only in ways no
    operator needs.

    MUST BE CALLED BEFORE `from config import Config`. config.Config is a class body,
    evaluated once at import, so a load afterwards changes nothing and looks like it
    worked -- which is the inert-fix shape this module was written to end.
    """
    env_path = ENV_FILE if path is None else path
    try:
        text = env_path.read_text()
    except OSError:
        return []
    loaded = []
    for name, value in assignments_in(text).items():
        if name in os.environ:
            continue
        os.environ[name] = value
        loaded.append(name)
    return sorted(loaded)
