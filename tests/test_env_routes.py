"""One sentence about how a value reaches a process, and NO second spelling of it.

Role: tests (pure; builds no adapter, opens no socket, reads source text only)
Reads: swap_terminal/env_routes.py and every .py in the tree, as TEXT
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHAT THIS FILE IS FOR, measured 2026-10-13. Eight messages in this tree ended with
"a value set only in .env ... does not reach here", and five of them were FALSE --
env_preamble.py had made every root tool load .env the day before, in the sentence
the loader exists to make accurate. One of the five, pay_test_deposit.py,
contradicted itself inside one file, 53 lines apart.

The sweep is in swap_terminal/env_routes.py's header with each site classified.
What is here is the part that keeps it fixed: an exhaustive scan asserting that
nothing spells the clause itself, so the ninth site cannot be written by copying
the eighth -- which is how all eight got there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains.registry import why_unconfigured  # noqa: E402
from chains.solana_payout_keypair import missing_keypair_refusal  # noqa: E402
from chains.xrp_payout_seed import missing_seed_refusal  # noqa: E402
from env_routes import how_a_value_arrives  # noqa: E402
from prose import code_without_prose  # noqa: E402

#: The claims that were false. Each is a SHAPE a future sentence could take, not a
#: whole line, because the eight differed in wording and agreed in the claim.
#:
#: WHY NOT JUST "does not reach here": two of the eight said "does not reach this
#: process" instead, and one said "Nothing in the serving path reads a .env". A scan
#: for one wording reports clean on the other two, which is rule 3's defect -- the
#: denominator is the patterns, not the language.
STALE_CLAIMS = (
    "does not reach here",
    "does not reach this process",
    "nothing in the serving path reads a .env",
    "nothing reads a .env here",
)

#: Sites allowed to contain a stale claim, with the reason, CHECKED rather than
#: assumed (rule 17).
ALLOWED = {
    "swap_terminal/env_routes.py": (
        "the module's own header quotes all four claims in order to classify them. The "
        "file that replaces a sentence is the one place the old sentence has to appear."
    ),
    "tests/test_env_routes.py": "this file, which lists them in STALE_CLAIMS",
    "swap_terminal/regtest/operator_panel.py": (
        "TRUE THERE, and measured: that file imports no config and calls no loader, so "
        "nothing puts .env into its environment. tests/test_env_preamble.py's sweep "
        "covers root tools that read Config and this is not one. It is the single site "
        "of the eight whose claim was correct, which is why the clause is not banned "
        "outright -- a ban would have forced a false correction here."
    ),
    "swap_terminal/services/wizard.py": (
        "a DOCSTRING quoting output as it was RENDERED to an unauthenticated reader, "
        "which is history. Rewriting a quoted measurement to match today's text would "
        "destroy the record of what was actually on the page (rule 1: the drift is the "
        "point)."
    ),
}


def _python_files():
    """Every .py in the tree except the virtualenv, as (repo-relative name, PATH).

    PATHS AND NOT TEXT, because prose.code_without_prose() takes a path -- it parses the
    file with ast and unparses it without docstrings, which it cannot do from a string it
    was handed. The first draft of this file passed the TEXT and the error was
    `OSError: File name too long` with a module's docstring in it: a wrong argument TYPE
    that the signature would have shown. Rule 17 at its smallest -- the helper's own
    docstring was one call away.
    """
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if ".venv" in relative or "/node_modules/" in relative:
            continue
        yield relative, path


def _code_by_file() -> dict[str, str]:
    """Every file's CODE, docstrings and comments removed, lowercased, computed ONCE.

    ONE PASS RATHER THAN FOUR. code_without_prose() runs ast.parse + ast.unparse per
    file, and parametrizing the gate over four claims ran the whole tree four times for
    one answer. The claims are looped inside the single test instead, which still names
    which claim and which file.
    """
    return {name: code_without_prose(path).lower() for name, path in _python_files()}


def test_the_sweep_finds_files_at_all():
    """The scan's own denominator. A rglob that matched nothing would report clean."""
    names = list(_code_by_file())

    assert len(names) > 100, f"only {len(names)} files scanned; the sweep is broken, not the tree"
    assert "swap_terminal/env_routes.py" in names
    assert "pay_test_deposit.py" in names, "the site that contradicted itself must be in scope"


def test_no_code_outside_the_registered_sites_claims_env_does_not_arrive():
    """THE GATE. The ninth site cannot be written by copying the eighth.

    CODE ONLY, through prose.code_without_prose(): a comment explaining the defect, or a
    docstring recording what a message used to say, is the fix rather than an instance of
    it. That distinction has cost this tree thirteen measured occurrences of a text scan
    matching its own explanation, which is what that helper exists for.

    ALL FOUR CLAIMS IN ONE REPORT, because a sweep that stops at the first failing claim
    tells somebody to fix one wording and leaves them to rediscover the other three --
    which is how the 2026-09-25 British-spelling scan reported 3 when the answer was 7.
    """
    code = _code_by_file()
    offenders = {
        claim: sorted(name for name, body in code.items()
                      if name not in ALLOWED and claim in body)
        for claim in STALE_CLAIMS
    }
    found = {claim: names for claim, names in offenders.items() if names}

    assert not found, (
        f"{found} claim in CODE that .env does not arrive. Five of the eight sites that said "
        f"this were false the day env_preamble.py landed -- every root tool loads .env. Call "
        f"env_routes.how_a_value_arrives() instead, or register the file in ALLOWED here with "
        f"the measurement that makes the claim true for it."
    )


@pytest.mark.parametrize("name,reason", sorted(ALLOWED.items()))
def test_every_registered_exception_is_still_a_real_file_with_a_reason(name, reason):
    """A register whose entries have gone stale is a ratchet (rule 19), not an exception."""
    assert (REPO_ROOT / name).is_file(), f"{name} is registered and does not exist"
    assert len(reason) > 40, f"{name}'s entry does not say WHY"


def test_operator_panels_claim_is_true_and_that_is_why_it_is_registered():
    """MUTATION: if that file ever loads .env, its sentence becomes the eighth defect.

    The ALLOWED entry asserts a property of another file, so it is checked here rather
    than trusted -- "I could not find a loader" is not "there is no loader" (rule 2).
    """
    code = code_without_prose(REPO_ROOT / "swap_terminal/regtest/operator_panel.py")

    assert "env_preamble" not in code, (
        "operator_panel.py now loads .env, so its 'Nothing reads a .env here' is false. "
        "Point it at env_routes.how_a_value_arrives() and drop its ALLOWED entry."
    )
    assert "load_into_environ" not in code
    assert "from config import" not in code and "import config" not in code, (
        "operator_panel.py now reads Config, so tests/test_env_preamble.py's sweep covers "
        "it and it must either load .env or be registered there"
    )


def test_the_armed_route_is_named_only_for_signing_material():
    """Conflating the two routes would describe a system this one deliberately is not.

    Ordinary configuration reaches the container through the web service's environment:
    block. SIGNING MATERIAL does not, by design -- only that chain's armed overlay
    carries it, and an unarmed container serves and cannot send. A message telling an
    operator to put XRP_PAYOUT_SECRET_SEED in .env and expect the container to sign
    would be wrong about the safety property, not just about a path.
    """
    plain = how_a_value_arrives("SOL_RPC_URL")
    armed = how_a_value_arrives("XRP_PAYOUT_SECRET_SEED", armed_chain="XRP")

    assert "environment: block" in plain
    assert "armed" not in plain, "an RPC url is not signing material and has no overlay"
    assert "docker-compose.web.armed-xrp.yml" in armed
    assert "--arm xrp" in armed
    assert "CANNOT send" in armed, "the default-unarmed safety property is not stated"
    assert "environment: block" not in armed, (
        "the armed sentence also offers the plain container route, which would be wrong: "
        "the environment: block does not carry a payout seed"
    )


def test_the_variable_is_named_only_when_the_caller_has_not_named_it():
    """Rule 14 asks for the parameter beside the number, not for it twice.

    chains/registry.why_unconfigured() names up to three variables in its own first
    sentence; repeating them produced "BTC_RPC_PORT, BTC_RPC_USER and BTC_RPC_PASS"
    twice in one paragraph on the first run of this change.
    """
    assert "for SOL_RPC_URL" in how_a_value_arrives("SOL_RPC_URL")
    bare = how_a_value_arrives()
    assert "THE ROUTES IN, and" in bare
    assert " for " not in bare.split(":")[0], "an empty variable left a dangling 'for'"


def test_every_caller_gets_a_sentence_that_names_env_as_a_working_route():
    """The positive form of the gate, over the five sites that were false.

    A scan for the stale claim passes the moment somebody deletes the clause entirely --
    which would leave an operator with no route named at all, and the compose side is the
    one they do not think of. So each corrected site is asserted to NAME .env.
    """
    sentences = {
        "why_unconfigured": why_unconfigured("BTC", {"BTC": {"port": 0, "user": "", "password": ""}}),
        "missing_seed_refusal": missing_seed_refusal(),
        "missing_keypair_refusal": missing_keypair_refusal(),
    }
    for where, sentence in sentences.items():
        assert ".env, which every root tool in this tree loads at startup" in sentence, (
            f"{where} no longer names .env as a route that works"
        )
        for claim in STALE_CLAIMS:
            assert claim not in sentence.lower(), f"{where} still carries {claim!r}"
