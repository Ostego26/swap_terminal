"""Importing this loads `.env` into os.environ. Import it BEFORE config, in a root tool.

Role: file (a preamble module, at the repository root per rule 10 so every root tool
      can import it with no path setup of its own)
Reads: `.env`, through env_file.load_into_environ()
Writes: os.environ, and ONLY names that are not already set
Can move funds: no. It sets environment variables. What a tool then DOES with them is
      that tool's business, and every fund-moving one still needs its own --apply.
Mainnet-safe: yes in itself -- and see THE POSTURE below, because what it loads can
      point a tool at a mainnet daemon if `.env` says so. It changes nothing about
      which network a value names; it only stops the value being invisible.

=========================================================================
THIS IS AN IMPORT WITH A SIDE EFFECT, WHICH RULE 12 NAMES AS A HAZARD
=========================================================================

Rule 12, verbatim: "a module that mutates `os.environ` or touches the filesystem when
imported makes every later import order-dependent." That is exactly what this does,
and it is why the file is named for the act rather than for a noun -- `import
env_preamble` reads as a step, not as a library.

THE ORDER-DEPENDENCE IS THE FEATURE HERE, which is the one case the hazard does not
argue against. config.Config is a class body evaluated once at import, so `.env` must
reach os.environ BEFORE `from config import Config` or the load changes nothing while
looking like it worked. Every caller NEEDS the ordering the rule warns about; what the
rule protects against is an ordering nobody asked for.

WHY NOT A FUNCTION CALL, which is what capture_daemon_commands.py and swap_stack.py do.
Because ruff's E402 permits a `sys.path` preamble and nothing else: a bare
`env_file.load_into_environ()` between the path insert and the imports flags EVERY
import after it. Measured on open_swap.py 2026-10-12: 20 errors, one per following
import, each needing its own `# noqa: E402`. Thirty-two root tools times that is a
noqa storm, and rule 19 is explicit that a noqa is a claim somebody checked -- hundreds
of them are hundreds of claims nobody will.

An import is allowed preamble, so this costs ONE line and no suppression at each call
site. The two tools that already do it as a call keep their call; they are correct and
their comments carry the measurement.

=========================================================================
WHAT IT FIXED, AND THE MEASUREMENT IS THE WHOLE JUSTIFICATION
=========================================================================

Measured 2026-10-12: **2 of 34 root tools loaded `.env`. The other 32 imported Config
and did not.** So the same `.env` produced different adapters depending on which tool
you ran, in the same shell, in the same second:

    swap_stack.py pairs    adapters here   GRC, ICP, LTC, SOL, XRP
    open_swap.py           adapters here   (none) -- no chain is configured in this process
                           REFUSED: GRC has no adapter in this process: GRC_RPC_PORT,
                           GRC_RPC_USER and GRC_RPC_PASS are unset (or 0) ...

The operator had just been told to use open_swap.py to rehearse a swap from the shell.
It could not see their configuration at all, and the refusal it printed named three
variables that were sitting in the `.env` two feet away. Thirty-two tools, every one of
them off by the same bootstrap line -- rule 8's drift, in the preamble rather than in
the logic.

=========================================================================
THE POSTURE, STATED RATHER THAN SLID IN
=========================================================================

Loading `.env` means a root tool now has credentials it previously lacked, and some of
those tools broadcast. That is a posture change and it is named here rather than left
for someone to discover:

  WHAT GUARDS A BROADCAST IS THE FLAG, NOT THE MISSING CREDENTIAL. open_swap.py is a
  dry run until `--apply`; capture_daemon_commands.py writes nothing without it;
  atomic_swap.py, pay_deposit.py and fund_desk.py each require an explicit act. A tool
  that refused for want of a variable was not SAFE, it was BROKEN -- and an operator
  working around it by exporting eighteen names by hand gets the same arming with a
  worse chance of getting one of them wrong.

  `.env` IS ALREADY THE DESK'S CONFIGURATION. `swap_stack.py on` hands it to docker
  compose, which arms the container's payout worker -- the thing that actually
  broadcasts, on a timer, with no flag. A file that arms the automatic payer and not
  the operator's own read-only balance check is the wrong way round.

  THE SERVING PATH STILL REFUSES, and that boundary is untouched. config.py declines to
  call load_dotenv at all, with a test over it: reading credentials at import would arm
  every process that imports Config, including gunicorn's. Nothing under
  swap_terminal/ imports this module, and tests/test_env_preamble.py asserts that.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# THE PATH INSERT IS HERE TOO, so a caller that imports this before its own insert
# still works. Idempotent in effect: a duplicate entry on sys.path costs one failed
# lookup and nothing else, and the alternative -- requiring callers to order two lines
# correctly -- is the ordering bug this module exists to remove.
if str(REPO_ROOT / "swap_terminal") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import env_file  # noqa: E402  -- after the path insert above, which is what makes it importable

#: The NAMES this import set, for a tool that wants to say so in its header (rule 14:
#: echo the parameters that decide the answer). NEVER THE VALUES -- `.env` holds
#: rpcpassword, SECRET_KEY and payout seeds, and load_into_environ() returns names for
#: exactly that reason.
LOADED_NAMES: list[str] = env_file.load_into_environ()
