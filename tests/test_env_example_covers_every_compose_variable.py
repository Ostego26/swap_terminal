"""Every variable the compose files read is in .env.example. One file, not a shell.

Role: test (reads docker-compose*.yml and .env.example; no socket, no database)
Reads: docker-compose*.yml, .env.example, .gitignore
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHY THIS GATE EXISTS, IN THE OPERATOR'S WORDS, 2026-10-10
=============================================================================

    "hey what the fuck? yeah, new shell should not rat fuck the entire fucking
     machine. it should persist. what the fuck"

    "why'd you fucking make such a fragile piece of trash that broke when a
     computer restart was required."

They opened a fresh terminal and the stack would not start:

    error while interpolating services.web.volumes.[]: required variable
    SWAP_DB_DIR is missing a value

MEASURED: the compose files read 91 environment variables and, until .env.example
existed, every one came from a shell export. One of the 91 blocks a bare
`docker compose up` outright. The other 90 fall back to defaults, which is the
WORSE failure: the stack comes up, builds no chain adapters because the RPC
credentials are empty, and the swap page reports chains as NOT CONFIGURED with
nothing anywhere saying the cause was a new terminal.

AND THE ANSWER ALREADY EXISTED IN THE TOOL. Docker Compose reads `.env` from the
project directory automatically, before interpolating anything, including
COMPOSE_FILE. This repository never used it.

WHY NOT, AND IT IS WORTH WRITING DOWN BECAUSE THE REASONING WAS HALF RIGHT.
config.py's header refuses `load_dotenv()`, and correctly: a module that mutates
os.environ at import makes every later import order-dependent, which is the
import-time side effect CLAUDE.md rule 12 names as a measured past defect. That
rule is about PYTHON. It got generalized into "no environment file anywhere",
which swept in compose's own `.env` -- a file compose reads itself and Python
never touches. A correct rule applied to a place it does not govern.

=============================================================================
WHAT THIS ASSERTS, AND WHY IT IS DERIVED RATHER THAN LISTED
=============================================================================

The variable set is computed from the compose files on every run. A checked-in
list would be a second spelling of the same fact (rule 8) and would drift the
first time somebody added a service -- silently, because a missing variable does
not fail a compose run, it falls back to a default.

So a variable added to any docker-compose*.yml and missing from .env.example
fails HERE, which is the only place it can fail cheaply.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from source_tree import REPOSITORY_ROOT

#: `${NAME}`, `${NAME:-default}` and `${NAME:?message}`. Upper-case only, which is
#: every variable these files use and which keeps `${1}`-style noise out.
COMPOSE_REFERENCE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(:-[^}]*|:\?[^}]*)?\}")

#: Compose reads these two ITSELF, out of .env, rather than interpolating them -- so
#: they are not `${...}` references anywhere and the scan below cannot find them.
#: COMPOSE_FILE is the one that matters: it is what lets a bare `docker compose up`
#: pick up the armed overlays instead of needing two -f flags, which is the ergonomic
#: defect measured on 2026-10-10 when the operator came up unarmed three times.
COMPOSE_OWN_VARIABLES = frozenset({"COMPOSE_FILE", "COMPOSE_PROJECT_NAME"})

ENV_EXAMPLE = REPOSITORY_ROOT / ".env.example"


def compose_files() -> list[Path]:
    return sorted(REPOSITORY_ROOT.glob("docker-compose*.yml"))


def referenced_variables() -> dict[str, set[str]]:
    """Every variable referenced in real yaml, to the files referencing it.

    COMMENT LINES ARE EXCLUDED, and that is not cosmetic: these compose files carry
    long explanatory comments that QUOTE example references -- `${VAR:?...}` and
    `${NAME}` both appear only inside prose. Counting them would put two variables
    that do not exist into the required set, and a reader filling in .env.example
    would go looking for what NAME configures.
    """
    found: dict[str, set[str]] = {}
    for path in compose_files():
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in COMPOSE_REFERENCE.finditer(line):
                found.setdefault(match.group(1), set()).add(path.name)
    return found


def example_variables() -> set[str]:
    """Every name .env.example mentions as an assignment, commented or not.

    A COMMENTED LINE COUNTS. The file deliberately leaves most variables commented
    with their compose default shown, so uncommenting one is the act of overriding
    it. A gate that only accepted live assignments would demand that every one of
    the 91 be set, which is the opposite of the point.
    """
    names = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.lstrip("#").strip()
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=", stripped)
        if match:
            names.add(match.group(1))
    return names


def test_there_is_something_to_check():
    """A gate over an empty scan passes while checking nothing."""
    assert compose_files(), "no docker-compose*.yml found; this gate is inert"
    found = referenced_variables()
    assert len(found) > 50, (
        f"the scan found {len(found)} compose variables and there were 91 when this gate was "
        f"written. A regex that stopped matching would make this pass by looking at nothing."
    )


def test_env_example_is_TRACKED_BY_GIT_and_not_merely_present():
    """PRESENT IS NOT SHIPPED, and this gate learned that the hard way within minutes.

    The first version of this file asserted `ENV_EXAMPLE.is_file()` and passed, because
    the template was sitting in the working tree. It was never committed: `.gitignore`
    line 24 is `.env.*`, which matches `.env.example`, so `git add -A` skipped it without
    a word. The commit went up with 13 files instead of 14 and the operator pulled a
    commit whose own message told them to run `cp .env.example .env` against a file that
    did not exist.

    THAT IS THIS SUITE'S RECURRING FAILURE SHAPE, three times in one day: a check that
    passes by looking at the wrong thing. A route asserted at a path that 404s, a
    constraint checked on an error page, and now a tracked-file claim checked against an
    untracked file. The fix each time is to assert the thing that actually has to be
    true -- here, that git knows about it.

    `git ls-files` RATHER THAN `check-ignore`, deliberately: the question is not whether
    a pattern would ignore it, it is whether the file is IN the repository. A later
    negation could be added and then removed, and only tracking survives that.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ENV_EXAMPLE.name],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False,
    )
    assert listed.returncode == 0, (
        f".env.example is not tracked by git, so nobody who clones or pulls this repository "
        f"gets it. `.gitignore` has `.env.*`, which matches it -- a `!.env.example` negation "
        f"after that line is what makes it trackable. git said: "
        f"{(listed.stderr or listed.stdout).strip()}"
    )
    assert ENV_EXAMPLE.is_file(), (
        ".env.example is missing. It is the file an operator copies to .env so the stack "
        "survives a new terminal -- see this module's docstring for what its absence cost."
    )
    assert len(ENV_EXAMPLE.read_text()) > 2000, "the template is too short to be the real one"


def test_every_compose_variable_is_in_the_template():
    """THE GATE. MUTATION: add `${NEW_THING:-x}` to any compose service.

    This is the assertion the file exists for. A variable that reaches a compose file
    without reaching the template is one an operator cannot discover except by reading
    yaml -- and because it has a default, nothing fails when they do not.
    """
    missing = sorted(set(referenced_variables()) - example_variables())
    assert not missing, (
        f"{len(missing)} variable(s) are read by a compose file and absent from .env.example: "
        f"{missing}. Add each one, with the default that compose file already carries, so an "
        f"operator can find it without reading yaml."
    )


def refused_if_empty() -> dict[str, set[str]]:
    """The variables compose declares `${NAME:?message}`, to the files declaring them.

    SEPARATE FROM referenced_variables() BECAUSE THE TWO ASK DIFFERENT QUESTIONS, and
    conflating them is what let .env.example ship broken. `${NAME:-default}` has an
    answer when nobody sets it. `${NAME:?}` has none: compose REFUSES TO INTERPOLATE,
    nothing starts, and -- the part that caught me -- it refuses an EMPTY value exactly
    as it refuses an unset one.
    """
    found: dict[str, set[str]] = {}
    for path in compose_files():
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in COMPOSE_REFERENCE.finditer(line):
                if (match.group(2) or "").startswith(":?"):
                    found.setdefault(match.group(1), set()).add(path.name)
    return found


def live_example_assignments() -> dict[str, str]:
    """Only the UNCOMMENTED assignments in .env.example, name -> value.

    THE COMMENTED ONES ARE DELIBERATELY EXCLUDED HERE, which is the opposite of
    example_variables() above, and the difference is the whole point of the gate
    below: a commented line sets nothing and is a correct way to show a variable that
    only one overlay needs. An UNCOMMENTED line with nothing after the `=` is the
    broken state, because `cp .env.example .env` turns it into a value compose sees
    and rejects.
    """
    live: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if match:
            live[match.group(1)] = match.group(2)
    return live


def test_the_template_is_USABLE_after_cp_and_not_merely_complete():
    """THE GATE THAT WAS MISSING. MUTATION: empty any uncommented value in .env.example.

    =========================================================================
    WHAT PASSED WHILE THE STACK COULD NOT START
    =========================================================================

    Every other test in this file passed on 2026-10-10 with
    `SWAP_DB_DIR=` -- uncommented, empty -- in the template. Coverage was complete:
    the name was there, nothing was orphaned, no secret value had leaked, git tracked
    it. And the operator ran `cp .env.example .env`, then `docker compose ps`, and got

        error while interpolating services.web.volumes.[]: required variable
        SWAP_DB_DIR is missing a value: [...1200 characters of refusal...]

    after a host restart, twenty minutes into trying to bring the stack up. Their
    words: "this is truly unfuckingbelievable".

    THE GAP BETWEEN THE OLD GATE AND THIS ONE IS COVERAGE VERSUS SATISFACTION. The
    tests above ask "is every variable MENTIONED". This asks "after the copy the
    README tells you to make, does compose interpolate" -- and only the second is the
    property an operator needs. That is this suite's recurring failure shape for the
    fourth time in one day: a check that passes by looking at the wrong thing (a
    route asserted at a path that 404s, a constraint checked on an error page, a
    tracked-file claim checked against an untracked file, and now a completeness
    check standing in for a usability one).

    =========================================================================
    WHY EMPTY AND COMMENTED ARE DIFFERENT, AND WHY ONLY ONE IS ALLOWED
    =========================================================================

      commented out    sets nothing. Compose falls back to its own `:-default`, or
                       refuses with its `:?` message if there is none AND the overlay
                       demanding it is in COMPOSE_FILE. Correct for every variable
                       only an armed overlay needs.
      uncommented
      and empty        sets the variable TO THE EMPTY STRING. `${NAME:?}` rejects
                       that identically to unset, and `${NAME:-default}` silently
                       takes the default -- so the line is either fatal or inert, and
                       never what the person writing it meant.

    So the rule is narrow and absolute: a `:?` variable may be commented, or set to
    something; it may not be present-and-empty.
    """
    required = refused_if_empty()
    assert required, "no `${VAR:?}` reference was found; this gate would be inert"

    live = live_example_assignments()
    broken = sorted(name for name, value in live.items() if name in required and not value.strip())
    assert not broken, (
        f"{broken} are declared `${{NAME:?}}` by a compose file and appear UNCOMMENTED AND EMPTY "
        f"in .env.example. `cp .env.example .env` then fails to interpolate and nothing starts. "
        f"Either give each a working default, or comment the line out so it sets nothing."
    )


def test_the_required_variable_a_bare_up_needs_has_a_default_that_fits_this_repository():
    """Non-empty is not enough: SWAP_DB_DIR has to point at the real database.

    The authority is <repo>/swap_terminal/swap_terminal.db and has been since
    config.py defined SWAP_DB_PATH, so `./swap_terminal` -- which compose resolves
    against the project directory -- is where it already is on a fresh clone. A
    template that interpolated cleanly and mounted the WRONG directory would start
    the stack against an empty database, and that is strictly worse than refusing:
    it is the 2026-10-01 two-database failure, shipped as a default.

    ASSERTED AGAINST THE FILESYSTEM, not against the string. Checking that the value
    equals "./swap_terminal" would pin my own typing; checking that it RESOLVES to the
    directory holding the database this repository configures is the property.
    """
    value = live_example_assignments().get("SWAP_DB_DIR", "")
    assert value.strip(), "SWAP_DB_DIR must be set in the template; see the gate above"

    resolved = (REPOSITORY_ROOT / value).resolve() if value.startswith(".") else Path(value).resolve()
    expected = (REPOSITORY_ROOT / "swap_terminal").resolve()
    assert resolved == expected, (
        f"SWAP_DB_DIR={value} resolves to {resolved}, and the database this repository "
        f"configures lives in {expected}. compose mounts this directory at /data, so a "
        f"wrong value starts the terminal against a different database than every root tool "
        f"reads."
    )
    assert resolved.is_dir(), f"{resolved} is not a directory in this checkout"


def test_the_template_names_nothing_the_compose_files_do_not_read():
    """The reverse direction: a template entry for a variable nothing reads is a lie.

    Rule 9: dead configuration gets read, copied from, and eventually set -- and a
    value an operator sets expecting an effect, which has none, is worse than a
    missing line.
    """
    orphans = sorted(example_variables() - set(referenced_variables()) - COMPOSE_OWN_VARIABLES)
    assert not orphans, (
        f"{orphans} appear in .env.example and are referenced by no compose file. Either a "
        f"compose file dropped them or the template invented them. COMPOSE_OWN_VARIABLES is "
        f"where a name compose reads ITSELF belongs."
    )


def test_COMPOSE_FILE_is_in_the_template_because_it_is_what_arms_a_bare_up():
    """The one line that is not a ${...} reference and matters most.

    Measured 2026-10-10: arming GRC meant two -f flags on every invocation, the
    operator came up unarmed three times, and a GRC payout then failed with
    "GRIDCOIN_WALLET_PASSPHRASE is not set in this process's environment" AFTER a
    customer's deposit had confirmed. COMPOSE_FILE in .env is what makes a bare
    `docker compose up` the armed one.
    """
    text = ENV_EXAMPLE.read_text()
    assert "COMPOSE_FILE=" in text, (
        "COMPOSE_FILE is not in .env.example, so a bare `docker compose up` reads only the "
        "default set and every armed overlay has to be named with -f on every invocation"
    )
    for overlay in ("armed-grc", "armed-sol", "armed-xrp"):
        assert overlay in text, (
            f"the template does not mention the {overlay} overlay, so an operator cannot tell "
            f"from it how to arm that chain"
        )


@pytest.mark.parametrize("pattern", [".env", ".env.*"])
def test_gitignore_blocks_the_real_env_file(pattern):
    """The template is checked in; the FILLED-IN one must never be.

    CLAUDE.md rule 2 records what this costs when it fails: a
    `.env.bak.2026-03-28_185451` is how a live GRIDCOIN_RPC_PASSWORD reached GitHub.
    `.env.example` holds no values, which is why it is the one that is tracked.
    """
    lines = {line.strip() for line in (REPOSITORY_ROOT / ".gitignore").read_text().splitlines()}
    assert pattern in lines, f".gitignore does not block {pattern!r}"


def test_the_template_itself_carries_no_secret_VALUE():
    """It is checked in, so a value in it is a published value.

    Asserts the shape rather than scanning for secrets: every SECRET-marked line must
    be an EMPTY assignment. A filled one would be a credential in git history, which
    no later deletion undoes.
    """
    lines = ENV_EXAMPLE.read_text().splitlines()
    for index, line in enumerate(lines):
        if "SECRET" not in line:
            continue
        for following in lines[index + 1: index + 3]:
            stripped = following.lstrip("#").strip()
            match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", stripped)
            if match:
                assert not match.group(2).strip(), (
                    f".env.example line {index + 2} assigns a value to {match.group(1)}, which "
                    f"is marked SECRET. This file is tracked, so that value would be published."
                )
                break
