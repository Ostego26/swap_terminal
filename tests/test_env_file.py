""".env reaches the host command, and the ordering that makes that true is asserted.

Role: test
Reads: swap_terminal/env_file.py, swap_stack.py as text
Writes: .env files under pytest's tmp_path only
Can move funds: no
Mainnet-safe: yes -- NO CHAIN, NO NETWORK. Every .env here is synthetic and every
       credential in one is the literal string "synthetic-not-real".

=============================================================================
THE DEFECT, measured on the operator's host 2026-10-12
=============================================================================

`swap_stack.py off` had just learned to stop the chain daemons, after the operator
asked three times. Their first run:

    3. chain daemons -- the operator asked for this twice
       BTC  NOT TOUCHED  BTC_RPC_PORT is unset, so this process builds no BTC adapter
       GRC  NOT TOUCHED  GRC_RPC_PORT is unset ...
       LTC  NOT TOUCHED  LTC_RPC_PORT is unset ...

while the container was talking to 19332 and 25715 in the same minute. The ports
were unset IN THE SHELL: `docker compose` reads `.env` by itself, a bare
`python3 swap_stack.py` does not. The feature shipped, passed its tests, and did
nothing -- the THIRD inert fix of that session, after a worker grace that shipped
as 15s and a connect-timeout claim that was wrong by a factor.

assets/swap-terminal.desktop had recorded the identical defect on 2026-10-09.

So this file pins the two things that make it stay fixed: the parse, and the
ORDER. config.Config is a class body evaluated once at import, so a load after
`from config import Config` mutates os.environ and changes nothing -- and looks
like it worked.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import env_file  # noqa: E402
from prose import code_without_prose  # noqa: E402

SWAP_STACK = REPO_ROOT / "swap_stack.py"


# --- the parse ------------------------------------------------------------------


def test_it_reads_plain_assignments():
    assert env_file.assignments_in("GRC_RPC_PORT=25779\n") == {"GRC_RPC_PORT": "25779"}


def test_it_strips_quotes_because_compose_does():
    """`GRC_RPC_PORT="25779"` reaches a container as 25779, not as a quoted string.

    A terminal whose two halves disagreed about that would build an adapter against
    port `"25779"` and fail at int() with a message about a literal.
    """
    assert env_file.assignments_in('A="25779"\n')["A"] == "25779"
    assert env_file.assignments_in("B='hunter2'\n")["B"] == "hunter2"


def test_it_ignores_comments_blanks_and_non_assignments():
    text = "# a comment\n\nnot an assignment\nlower_case=skipped\nGOOD=1\n"
    assert env_file.assignments_in(text) == {"GOOD": "1"}


def test_leading_whitespace_does_not_hide_an_assignment():
    assert env_file.assignments_in("   GRC_RPC_PORT=25779\n") == {"GRC_RPC_PORT": "25779"}


def test_an_empty_value_is_a_value_and_not_a_missing_name():
    """`.env.example` ships commented `NAME=` lines, and configure_env writes real ones.

    An empty string reaching the environment is what config.py's own header records
    going wrong once -- "it wrote `export GRC_RPC_PORT=''` -- five empty exports" and
    an int() traceback -- so this must round-trip as present-and-empty rather than
    being silently dropped into "unset".
    """
    assert env_file.assignments_in("EMPTY=\n") == {"EMPTY": ""}


# --- the load -------------------------------------------------------------------


def test_the_shell_WINS_over_the_file(tmp_path, monkeypatch):
    """compose gives an exported variable precedence, so this must too.

    An operator who exports GRC_RPC_PORT for one command gets that value in the
    container and must get the same value here, or the two halves of one terminal
    address different daemons. With three Gridcoin wallets on that host the cost of
    disagreeing is `off` stopping one the container never used.
    """
    path = tmp_path / ".env"
    path.write_text("GRC_RPC_PORT=25779\n")
    monkeypatch.setenv("GRC_RPC_PORT", "25715")
    loaded = env_file.load_into_environ(path)
    assert os.environ["GRC_RPC_PORT"] == "25715", "the file overwrote an exported value"
    assert "GRC_RPC_PORT" not in loaded


def test_it_sets_what_the_shell_does_not_have(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("LTC_RPC_PORT=19332\n")
    monkeypatch.delenv("LTC_RPC_PORT", raising=False)
    assert env_file.load_into_environ(path) == ["LTC_RPC_PORT"]
    assert os.environ["LTC_RPC_PORT"] == "19332"


def test_a_missing_file_is_not_an_error(tmp_path):
    """A fresh checkout has no .env until configure_env.py writes one."""
    assert env_file.load_into_environ(tmp_path / "does-not-exist") == []


def test_it_returns_NAMES_and_never_values(tmp_path, monkeypatch):
    """`.env` holds rpcpassword, SECRET_KEY and payout seeds. Rule 14: never print one."""
    path = tmp_path / ".env"
    path.write_text("GRC_RPC_PASS=synthetic-not-real\n")
    monkeypatch.delenv("GRC_RPC_PASS", raising=False)
    loaded = env_file.load_into_environ(path)
    assert loaded == ["GRC_RPC_PASS"]
    assert "synthetic-not-real" not in str(loaded)


# --- the ordering, which is the half a comment cannot hold -----------------------


def test_swap_stack_loads_the_env_BEFORE_it_imports_config():
    """THE ASSERTION THE OPERATOR'S INERT RUN EARNED.

    config.Config is a class body, evaluated once at import. A load afterwards is a
    no-op that looks like a fix. This reads swap_stack.py's own source, because the
    constraint is an ORDER and no runtime check can see it after the fact.

    MUTATION: move the load_into_environ() line below the config import. This fails
    and names both line numbers.
    """
    lines = SWAP_STACK.read_text().splitlines()
    load_at = next(
        (i for i, line in enumerate(lines) if "env_file.load_into_environ()" in line), None
    )
    config_at = next(
        (i for i, line in enumerate(lines) if line.startswith("from config import Config")), None
    )
    assert load_at is not None, "swap_stack.py no longer loads .env at all"
    assert config_at is not None, "swap_stack.py no longer imports Config"
    assert load_at < config_at, (
        f"swap_stack.py calls env_file.load_into_environ() at line {load_at + 1} but imports "
        f"Config at line {config_at + 1}. Config is a class body read once at import, so the "
        f"load changes nothing -- which is the inert-fix shape measured on the operator's host "
        f"2026-10-12, where `off` reported every chain unset while the container used them."
    )


def test_config_py_still_does_NOT_load_dotenv_itself():
    """Its own header refuses, and the refusal is right -- rule 12's import-time side effect.

    A module that mutates os.environ when imported makes every later import
    order-dependent, and ~90 modules here import config. The load belongs to an entry
    point, which has one import order and owns it. This gate stops somebody "fixing"
    the problem by moving it into the library.
    """
    # code_without_prose() AND NOT read_text(), because config.py's header NAMES
    # load_dotenv in the sentence refusing to call it -- so a raw scan fails the file
    # for explaining itself. TENTH occurrence of that defect class in this tree, and
    # tests/prose.py was written for it one hour before this test was.
    source = code_without_prose(REPO_ROOT / "swap_terminal" / "config.py")
    assert "load_dotenv" not in source, (
        "config.py now loads .env at import. Its header refuses this for the reason rule 12 "
        "gives, and swap_stack.py is where the load belongs."
    )


def test_env_file_does_nothing_on_import():
    """The whole design: importing is free, the side effect must be CALLED.

    Asserted over the module's own AST rather than by importing it and hoping -- a
    module-level call is what would make this file as dangerous as the thing it
    replaces.
    """
    tree = ast.parse((REPO_ROOT / "swap_terminal" / "env_file.py").read_text())
    calls = [
        node for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert calls == [], "env_file.py calls something at module level, which is the defect it exists to avoid"
