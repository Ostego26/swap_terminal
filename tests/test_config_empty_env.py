"""A set-but-empty environment variable must mean absent, not crash the import.

Role: test (subprocesses only; opens no socket, touches no wallet)
Reads: config.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

SUBPROCESSES, BECAUSE config.Config READS THE ENVIRONMENT AT CLASS-DEFINITION TIME.
tests/conftest.py says this already: the values are baked in when the module is
imported, so monkeypatch.setenv inside a test is too late. A fresh interpreter with
a modified environment is the only way to exercise this, and it is also exactly how
the failure reached the operator -- they sourced a file and ran a command.

THE RUN THIS COMES FROM, 2026-09-26. A generator command wrote an env file from a
shell that did not hold the values, so it wrote five `export NAME=''` lines. Sourcing
it produced

    ValueError: invalid literal for int() with base 10: ''

from config.py at import, killing open_swap.py, both workers and app.py before any
of them could say a word. os.getenv returns "" for a variable set to nothing, so the
two-argument default never applied.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "swap_terminal"

# The five the operator's env file named. Not a guess: these are the variables that
# generator wrote, and GRC_RPC_PORT is the one whose int() raised.
OPERATOR_ENV_FILE_VARIABLES = (
    "GRC_RPC_PORT",
    "GRC_RPC_USER",
    "GRC_RPC_PASS",
    "XRP_RPC_URL",
    "XRP_DEPOSIT_ACCOUNT",
)


def config_in_fresh_interpreter(env_overrides: dict, body: str) -> subprocess.CompletedProcess:
    """Import config.py in a new interpreter with `env_overrides` applied."""
    environment = dict(os.environ)
    environment.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(APP_ROOT)!r})\n{body}"],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        check=False,
    )


def test_five_empty_exports_do_not_crash_the_import():
    """THE OPERATOR'S EXACT FAILURE. MUTATION: put int(os.getenv(...)) back."""
    result = config_in_fresh_interpreter(
        dict.fromkeys(OPERATOR_ENV_FILE_VARIABLES, ""),
        "from config import Config\nprint('PORT', Config.RPC['GRC']['port'])\n",
    )

    assert "ValueError" not in result.stderr, f"still crashes: {result.stderr[-500:]!r}"
    assert result.returncode == 0
    assert "PORT 0" in result.stdout, "an empty port must read as UNCONFIGURED_PORT, not raise"


def test_an_empty_value_reaches_the_same_refusal_as_an_unset_one():
    """Not merely "does not crash": it must land in the path that NAMES the variable.

    Everything built today -- chains/registry.build_adapters() skipping the chain,
    the swap page badging the pair DISABLED, create_swap()'s refusal, /api/health's
    missing_settings -- keys off the value being falsy. An empty string that
    survived as "" would be truthy in some of those and falsy in others, which is
    worse than either.
    """
    result = config_in_fresh_interpreter(
        dict.fromkeys(OPERATOR_ENV_FILE_VARIABLES, ""),
        "from config import Config\n"
        "from chains.registry import build_adapters, missing_settings\n"
        "print('ADAPTERS', sorted(build_adapters(Config.RPC)))\n"
        "print('MISSING', missing_settings(Config.RPC, 'GRC'))\n",
    )

    assert "ADAPTERS []" in result.stdout
    assert "MISSING ['GRC_RPC_PORT', 'GRC_RPC_USER', 'GRC_RPC_PASS']" in result.stdout


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t"])
def test_whitespace_only_is_the_same_mistake_with_a_space_in_it(blank):
    """int(" ") raises exactly as int("") does, and `export PORT=" "` is a real typo.

    MUTATION: drop the .strip() from _env(). Only the whitespace cases fail, which
    is why they are parameterized separately from the empty one.
    """
    result = config_in_fresh_interpreter(
        {"GRC_RPC_PORT": blank},
        "from config import Config\nprint('PORT', Config.RPC['GRC']['port'])\n",
    )

    assert "PORT 0" in result.stdout, f"blank={blank!r} stderr={result.stderr[-300:]!r}"


def test_a_real_value_still_wins():
    """So the fix cannot pass by ignoring the environment entirely."""
    result = config_in_fresh_interpreter(
        {"GRC_RPC_PORT": "25715"},
        "from config import Config\nprint('PORT', Config.RPC['GRC']['port'])\n",
    )

    assert "PORT 25715" in result.stdout


def test_every_typed_read_in_config_survives_an_empty_value():
    """Not just the one that bit. 27 typed reads, any of which would raise.

    The variable names are derived from config.py's own source rather than listed
    here -- a hand-written list would be a second copy that passes while a new
    setting is added without the helper (rule 8). Every _env_int/_env_float name in
    the file is set to "" at once, and the import must still succeed.
    """
    source = (APP_ROOT / "config.py").read_text()
    typed = re.findall(r'_env_(?:int|float)\("([A-Z0-9_]+)"', source)
    assert len(typed) >= 20, f"expected the file's typed reads, found {len(typed)}: {typed}"

    result = config_in_fresh_interpreter(
        dict.fromkeys(typed, ""),
        "from config import Config\nprint('IMPORTED', len(Config.RPC))\n",
    )

    assert result.returncode == 0, (
        f"importing with all {len(typed)} typed variables empty failed:\n{result.stderr[-800:]}"
    )
    assert "IMPORTED" in result.stdout


def test_an_unparseable_value_still_raises():
    """The fix must not swallow a REAL error. `GRC_RPC_PORT=banana` is a mistake
    that has to be reported, not defaulted away to 0 -- silently treating it as
    unconfigured would mean an operator who fat-fingered a port gets "not
    configured" and goes looking for an unset variable that is set."""
    result = config_in_fresh_interpreter(
        {"GRC_RPC_PORT": "banana"},
        "from config import Config\nprint('PORT', Config.RPC['GRC']['port'])\n",
    )

    assert result.returncode != 0
    assert "banana" in result.stderr, "the bad value must appear in the error"
