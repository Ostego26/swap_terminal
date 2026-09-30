"""The parts of solana_chain_check.py that can be proven without a cluster.

Role: test (pure pieces of the operator diagnostic; no chain, no socket)
Reads: solana_chain_check.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY ONLY THE PARTS. solana_chain_check.py's whole purpose is to meet a real
Solana cluster, and its value is precisely the thing that cannot be tested from
here -- whether the RPC response field names chains/solana.py expects are the
ones a cluster actually sends. Measured 2026-09-25: no Solana endpoint was
reachable from this environment (api.devnet.solana.com returns 403 from the
proxy) and solana-test-validator could not be installed.

So this covers what is provable without one: the genesis-hash-to-network
mapping, the exit codes, and the properties rule 14 asks of the output. The
same split tests/test_regtest_harness_units.py already makes for
regtest_htlc_verify.py -- "the parts that can be proven without a daemon."

THE GENESIS MAPPING IS THE ONE WORTH HAVING. The script identifies the network
from getGenesisHash rather than from the URL, because a cluster cannot lie
about its genesis and a hostname can. An operator with a "devnet" alias pointed
at mainnet would otherwise read the word devnet all the way to a real transfer.
If a hash in that table were wrong, mainnet would print as devnet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from solana_chain_check import (  # noqa: E402 -- the sys.path line above is what puts the repository root on the path; this script lives there (rule 10), not inside the package.
    GENESIS_HASHES,
    _network_line,
    check_rent,
    make_runner,
    print_banner,
    print_summary,
    what_the_hunt_established,
)


class FakeAdapter:
    """Only what _network_line touches. Nothing here opens a socket."""

    def __init__(self, genesis):
        self.genesis = genesis

    def call(self, method, *_params):
        assert method == "getGenesisHash"
        return self.genesis


def test_mainnet_is_identified_and_is_labeled_real_money():
    """The label has to carry the consequence, not just the name. An operator
    skimming reads REAL MONEY; 'mainnet-beta' is a word they may not parse."""
    mainnet = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
    line = _network_line(FakeAdapter(mainnet))
    assert "MAINNET" in line
    assert "REAL MONEY" in line
    assert mainnet in line


@pytest.mark.parametrize(
    ("genesis", "expected"),
    [
        ("EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG", "DEVNET"),
        ("4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY", "TESTNET"),
    ],
)
def test_the_public_test_clusters_are_identified(genesis, expected):
    assert expected in _network_line(FakeAdapter(genesis))


def test_an_unknown_genesis_says_so_rather_than_guessing():
    """A local solana-test-validator has its own genesis every time it is wiped,
    so 'unrecognized' is the ORDINARY answer there and must read as one rather
    than as an alarm -- while still never being reported as a named network."""
    line = _network_line(FakeAdapter("SomeLocalValidatorGenesisHashThatIsNotPublic"))
    assert "UNRECOGNIZED" in line
    assert "solana-test-validator" in line
    assert not any(name.split()[0] in line for name in GENESIS_HASHES.values())


def test_every_genesis_in_the_table_is_distinct():
    """Two networks sharing a hash would make one of them print as the other."""
    assert len(set(GENESIS_HASHES)) == len(GENESIS_HASHES)
    assert len(set(GENESIS_HASHES.values())) == len(GENESIS_HASHES)


# --- exit codes and output shape ---------------------------------------------


def test_a_clean_run_exits_zero_and_still_says_what_was_not_proven(capsys):
    """Rule 14: a pass must not imply more than it established. Nothing was sent,
    and the summary says so rather than leaving 'PASSED' to be read as 'ready'."""
    assert print_summary([], 3.0) == 0
    out = capsys.readouterr().out
    assert "PASSED" in out
    assert "NOT PROVEN" in out
    assert "refuse by design" in out
    # Rule 6: the elapsed figure is microfortnights with the seconds beside it.
    assert "µfn" in out
    assert "ufn" not in out.replace("µfn", "")


def test_a_failing_run_exits_one_and_names_every_step_that_failed(capsys):
    assert print_summary(["getHealth", "getSlot"], 1.0) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "getHealth" in out
    assert "getSlot" in out
    assert "PASSED" not in out


def test_a_failed_step_is_recorded_rather_than_ending_the_run(capsys):
    """One broken step must not hide the other nine -- but it must also not be
    swallowed. The runner catches, NAMES, prints and counts, and the count is
    what becomes the exit code."""
    failures: list[str] = []
    run = make_runner(failures)

    def boom():
        raise RuntimeError("the shape was not what was expected")

    run("getWhatever", "an expectation", boom)
    run("getSomethingElse", "another", lambda: "fine")
    out = capsys.readouterr().out
    assert failures == ["getWhatever"]
    assert "FAIL" in out
    assert "the shape was not what was expected" in out
    # The second step still ran, which is the point of catching at all.
    assert "fine" in out


def test_every_step_announces_before_it_runs(capsys):
    """Rule 14's first clause. A line that only appears on completion is
    invisible during the wait, which is exactly when the operator is deciding
    whether to Ctrl-C -- and here that is a diagnostic, but the habit is the
    one that keeps somebody from killing a live cycle."""
    run = make_runner([])
    run("someStep", "what it should say", lambda: "answered")
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0].strip().startswith("someStep ...")
    assert "what it should say" in lines[0]
    assert "answered" in lines[1]


def test_the_banner_states_the_network_relevant_parameters_before_anything_runs(capsys):
    rpc = {"url": "http://x.invalid", "mint": "", "min_commitment_rank": 3}
    print_banner(rpc, "")
    out = capsys.readouterr().out
    assert "READ-ONLY" in out
    assert "http://x.invalid" in out
    # An absent value prints as a described absence, never as a blank gap.
    assert "(none -- checking native SOL)" in out
    # And the threshold says what it is, beside the number (rule 14).
    assert "NOT blocks" in out


def test_an_unset_endpoint_is_reported_rather_than_silently_doing_nothing(capsys):
    rpc = {"url": "", "mint": "", "min_commitment_rank": 3}
    print_banner(rpc, "")
    assert "SOL_RPC_URL is UNSET" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What a hunt ESTABLISHED, which is not the same question as what it read.
# ---------------------------------------------------------------------------
def test_a_memo_we_parsed_CONFIRMS_the_program_id():
    confirmed, lines = what_the_hunt_established("MemoX", seen=3, read=9, unread=11)
    assert confirmed is True
    assert "CONFIRMED" in " ".join(lines)


def test_ZERO_READ_ESTABLISHES_NOTHING_AND_MUST_NOT_READ_AS_A_WRONG_ID():
    """THE DEFECT THIS PINS PRINTED A FALSEHOOD TO THE OPERATOR ON 2026-09-29.

    `solana_chain_check.py --hunt-memo 20` against api.devnet.solana.com got HTTP 429 on all
    20 transactions for the second program id. With only two branches, read==0 fell into the
    "we looked and found nothing" arm and printed

        read transactions for this id and found NO memo our parser recognizes.
        ... If it says jsonParsed, the id is wrong.

    Nothing had been read. A reader following that guidance would have changed a constant
    that may well be correct -- which is rule 17's register error rendered as output: a
    hypothesis printed where a measurement belongs, in the one file whose whole job is to
    tell "the cluster is quiet" apart from "our constant is wrong".

    MUTATION: delete the `read == 0` branch and this fails on the phrase it must not print.
    """
    confirmed, lines = what_the_hunt_established("Memo1", seen=0, read=0, unread=20)
    text = " ".join(lines)
    assert confirmed is False
    assert "NOT ESTABLISHED" in text
    assert "neither that it is right" in text
    assert "429" in text, "the operator needs to know throttling is the cause, not the id"
    assert "the id is wrong" not in text, (
        "zero transactions read cannot support a claim about the program id, and this is the "
        "exact sentence the defect printed"
    )


def test_reading_transactions_and_finding_no_memo_IS_evidence_and_says_so():
    """The third outcome, and the one that genuinely is about the id."""
    confirmed, lines = what_the_hunt_established("Memo1", seen=0, read=20, unread=0)
    text = " ".join(lines)
    assert confirmed is False
    assert "found NO memo our parser recognizes" in text
    assert "the id is wrong" in text, "with transactions actually read, that reading IS supported"


def test_the_three_outcomes_are_distinguishable_from_each_other():
    """Rule 14: "did nothing" must not look like "did work". Asserted on the OUTPUT, since
    the output is what the operator reads."""
    texts = [
        " ".join(what_the_hunt_established("M", seen=1, read=1, unread=0)[1]),
        " ".join(what_the_hunt_established("M", seen=0, read=0, unread=1)[1]),
        " ".join(what_the_hunt_established("M", seen=0, read=1, unread=0)[1]),
    ]
    assert len(set(texts)) == 3, "two outcomes render identically, which is the defect"


# ---------------------------------------------------------------------------
# WHICH HALF OF THE RENT REFERENCE IS MEASURED.
#
# The header printed above every rent check said "mainnet-beta, devnet and testnet all
# reached it on SIMD-0437 step 2, 2026-09-11" -- as a statement of fact, in the line an
# operator reads on every run. chains/solana_units.py and the mismatch line in this same
# file both mark that figure as MEASURED on devnet and SOURCED for the other two, and no
# mainnet-beta or testnet endpoint has ever been read from this tree.
#
# Rule 17 is exactly that a reason to believe and a measurement must not share a voice.
# Two of the three sites hedged and the third did not, so the third was wrong -- and it
# was the only one rendered to a screen.
# ---------------------------------------------------------------------------


def _rent_header(capsys) -> str:
    """The RENT header, produced by the real function with a stub that answers nothing.

    check_rent() prints the header and then runs two steps; the steps are handed a `run`
    that records instead of calling, so this exercises the real print without an endpoint.
    """
    check_rent(None, lambda *args, **kwargs: None)
    return capsys.readouterr().out


def test_the_rent_header_says_which_cluster_the_figure_was_MEASURED_on(capsys):
    """MUTATION: restore "all reached it". The two assertions below fail together.

    A reader who takes the mainnet-beta figure as measured sizes a mainnet rent-exemption
    against a number nobody read, and the reading that would correct them -- a mismatch --
    is the one the header tells them to expect and ignore.
    """
    header = _rent_header(capsys)
    assert "MEASURED on devnet" in header, "the header does not say where the figure was measured"
    assert "SOURCED" in header, "the header does not mark the unmeasured clusters as sourced"
    assert "all reached it" not in header, (
        "the header states as fact that three clusters carry this figure, which two other sites "
        "in this tree correctly mark as sourced for two of them"
    )


def test_the_rent_header_still_says_a_mismatch_is_EXPECTED(capsys):
    """The reason the header exists, which the correction must not cost.

    SIMD-0437 has five steps and three are still to come, so this number WILL go stale by
    design. An operator who reads a mismatch as a defect goes looking for a bug in the
    adapter; the header is what stops that, and it also has to say the chain is the
    authority so nobody sizes a transfer from the constant.
    """
    header = _rent_header(capsys)
    assert "EXPECTED, not a defect" in header
    assert "chain is the authority" in header
    assert "SIMD-0437" in header, "the header does not name what moves the number"
