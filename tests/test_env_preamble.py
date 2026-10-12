#!/usr/bin/env python3
"""Every root tool that reads Config must load `.env` first, or say why not.

Role: tests (read-only; parses the root tools' source, imports none of them)
Reads: the *.py files at the repository root, and env_preamble.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE DEFECT THIS GATE IS FOR, MEASURED 2026-10-12.

`swap_stack.py pairs` and `open_swap.py` were run in the same shell, in the same
minute, against the same `.env`:

    swap_stack.py pairs    adapters here   GRC, ICP, LTC, SOL, XRP
    open_swap.py           adapters here   (none) -- no chain is configured in this process
                           REFUSED: GRC has no adapter in this process: GRC_RPC_PORT,
                           GRC_RPC_USER and GRC_RPC_PASS are unset (or 0) ...

**2 of 34 root tools loaded `.env`; 32 imported config.Config and did not.** config.Config
is a class body evaluated once at import, so the load has to happen before that import or
it changes nothing -- and thirty-two tools had no load at all. The operator had just been
told to rehearse a swap with open_swap.py; it could not see their configuration, and the
refusal it printed named three variables sitting in the `.env` two feet away.

That is rule 8's drift in the BOOTSTRAP rather than in any logic, which is the reason a
test is the fix and not thirty-two careful edits: the next root tool will be written by
copying one of these files, and whether it works will depend on which one.

WHY A REGISTER RATHER THAN A RULE. Two files must NOT load it, and both for stated
reasons -- the serving path, where config.py's refusal to call load_dotenv exists
precisely so that importing Config never arms a process, and the .env WRITER, which has
to be able to tell a value that exists only in the file from one exported in the shell.
A rule with two exceptions is a register with two entries, and this tree already settles
that argument the same way in tests/test_daemon_conf.py: "I tried three rules first and
each one bent."

NOT A RATCHET (rule 19). A ratchet tolerates a backlog; this tolerates nothing. Every
root tool reading Config is either loading `.env` or named below with a reason, the two
sets are asserted to be exhaustive AND disjoint, and a new file lands in neither until
somebody decides which.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The two root tools that must NOT load `.env`, and why. The reason is the entry: a bare
#: name here would be a suppression, and rule 19 is explicit that a suppression is a claim
#: somebody checked.
MUST_NOT_LOAD = {
    "gunicorn.conf.py": (
        "THE SERVING PATH. This is gunicorn's own config, read inside the container where "
        "compose supplies the environment. config.py declines to call load_dotenv at all, "
        "with a test over it, because reading credentials at import would arm every process "
        "that imports Config -- and this is the process that refusal is about."
    ),
    "configure_env.py": (
        "THE WRITER. configure_env.write_env() is the one thing in this tree that edits .env "
        "(rule 8), and it shows the operator what is set now. Loading the file into the "
        "environ first would make a value that exists ONLY in .env indistinguishable from one "
        "exported in their shell, which is the distinction this tool exists to manage."
    ),
}

#: The two ways a root tool may load it, and both are correct. The preamble IMPORT is the
#: one that scales -- see env_preamble's header on ruff's E402 and the twenty suppressions
#: a bare call cost in open_swap.py -- and the explicit CALL is what swap_stack.py and
#: capture_daemon_commands.py did first, with the measurement in their own comments.
_LOADS = ("import env_preamble", "env_file.load_into_environ()")


def root_tools_reading_config() -> dict[str, str]:
    """{filename: source} for every root .py that imports config.Config.

    PARSED, NOT IMPORTED, deliberately: importing thirty-four root tools would run
    thirty-four sys.path inserts and -- for the ones under test -- thirty-four `.env`
    loads, in one pytest process, in an order nothing controls. The question here is
    structural and the answer is in the text.
    """
    found = {}
    for path in sorted(REPO_ROOT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if re.search(r"^from config import", source, re.MULTILINE):
            found[path.name] = source
    return found


def test_the_sweep_finds_the_tools_it_is_meant_to_measure():
    """A gate that measures nothing passes forever.

    The denominator is stated (rule 3) and the two named files must be IN it -- if either
    stopped importing Config, its register entry would be dead prose claiming to protect
    something.
    """
    found = root_tools_reading_config()

    assert len(found) >= 30, (
        f"only {len(found)} root tools import config.Config; 34 did on 2026-10-12, so "
        f"either this sweep broke or the tree changed shape"
    )
    for name in MUST_NOT_LOAD:
        assert name in found, (
            f"{name} is registered as MUST NOT LOAD but no longer imports Config, so its "
            f"entry protects nothing and should be deleted"
        )


def test_every_root_tool_reading_config_loads_env_or_is_registered():
    """THE GATE. A new root tool lands in neither set until somebody decides which.

    This is the whole fix: the next one will be written by copying an existing file, and
    before today whether it worked depended on which file got copied.
    """
    unaccounted = []
    for name, source in root_tools_reading_config().items():
        if name in MUST_NOT_LOAD:
            continue
        if not any(marker in source for marker in _LOADS):
            unaccounted.append(name)

    assert not unaccounted, (
        f"{sorted(unaccounted)} import config.Config and never load .env, so they see none "
        f"of the operator's configuration and will refuse naming variables that are set. "
        f"Add `import env_preamble` immediately after the sys.path line (see any patched "
        f"tool), or add an entry to MUST_NOT_LOAD here with the reason."
    )


@pytest.mark.parametrize("name", sorted(MUST_NOT_LOAD))
def test_the_registered_exceptions_really_do_not_load_it(name):
    """Both directions. A register entry is a claim, and a claim gets checked (rule 17)."""
    source = (REPO_ROOT / name).read_text(encoding="utf-8")

    for marker in _LOADS:
        assert marker not in source, (
            f"{name} is registered as MUST NOT LOAD and contains {marker!r}. "
            f"{MUST_NOT_LOAD[name]}"
        )


def test_the_preamble_loads_before_config_is_imported_in_every_tool_that_uses_it():
    """ORDER IS THE WHOLE MECHANISM, and getting it wrong is invisible.

    config.Config is a class body evaluated once at import. A load after that import
    changes nothing and looks exactly like a load that worked -- which is the inert-fix
    shape env_file's own docstring was written to end, and the shape this session has
    already produced once in a different file.
    """
    checked = 0
    for name, source in root_tools_reading_config().items():
        if "import env_preamble" not in source:
            continue
        preamble_at = source.index("import env_preamble")
        config_at = re.search(r"^from config import", source, re.MULTILINE).start()
        assert preamble_at < config_at, (
            f"{name} imports env_preamble AFTER `from config import Config`, so the load is "
            f"inert: Config's class body has already read the environment"
        )
        checked += 1
    assert checked >= 25, f"only {checked} tools use the preamble; the sweep is not measuring"


def test_the_preamble_returns_names_and_never_values():
    """`.env` holds rpcpassword, SECRET_KEY and payout seeds.

    LOADED_NAMES is for a tool that wants to say how many names it set (rule 14). A value
    reaching it would reach whatever printed it, and this repository has already published
    one live credential by committing a file (CLAUDE.md rule 2 has that story).
    """
    import env_preamble  # noqa: PLC0415 -- importing it IS the behavior under test

    assert isinstance(env_preamble.LOADED_NAMES, list)
    for name in env_preamble.LOADED_NAMES:
        assert isinstance(name, str)
        assert "=" not in name, f"{name!r} looks like an assignment rather than a NAME"


def test_nothing_under_swap_terminal_imports_the_preamble():
    """The serving path's boundary, asserted rather than trusted.

    config.py refuses load_dotenv so that importing Config can never arm a process. A
    module under swap_terminal/ importing env_preamble would route around that refusal
    for every process that imports it -- gunicorn's four included.
    """
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "swap_terminal").rglob("*.py"))
        if "import env_preamble" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, (
        f"{offenders} import env_preamble from inside the package. It is a ROOT-TOOL "
        f"preamble: importing it under swap_terminal/ would load .env into any process "
        f"that imports that module, which is exactly what config.py's refusal to call "
        f"load_dotenv prevents."
    )
