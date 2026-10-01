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

import json as _json
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import chains.solana as chains_solana  # noqa: E402
import chains.solana as solana_module  # noqa: E402
import chains.solana_memo as chains_solana_memo  # noqa: E402
from chains.solana import SolanaRPCError  # noqa: E402
from chains.solana_address import SOLANA_DEVNET_ACCOUNT, is_valid_address  # noqa: E402
from chains.solana_memo import MEASURED_MEMO_PROGRAM_IDS, MEMO_PROGRAM_IDS  # noqa: E402
from chains.solana_units import DISCOVERY_COMMITMENT  # noqa: E402

import solana_chain_check  # noqa: E402
from solana_chain_check import (  # noqa: E402 -- the sys.path line above is what puts the repository root on the path; this script lives there (rule 10), not inside the package.
    GENESIS_HASHES,
    MEMO_HUNT_GIVE_UP_AFTER_THROTTLES,
    MEMO_HUNT_TRANSACTION_VERSION,
    RPC_RETRIES_PER_CALL,
    CreditPathObserved,
    _deposits_line,
    _network_line,
    call_with_backoff,
    check_rent,
    credit_path_lines,
    find_a_holder,
    holder_from_mint_traffic,
    hunt_one_program_id,
    make_runner,
    memo_status_lines,
    owner_in_post_token_balances,
    owner_of,
    print_banner,
    print_summary,
    read_one_transaction,
    resolve_address,
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

    RESEEDED 2026-09-30 AS `throttled=20` RATHER THAN `unread=20`, and the reason is the
    defect this file now also covers. Those twenty were HTTP 429s -- the docstring above has
    always said so -- but the code counted them as `unread`, so this test had to seed them as
    unread to reproduce the case it is about. A rate limit is not an unreadable transaction,
    and once the two are counted apart the seed can say what actually happened.

    MUTATION: delete the `read == 0` branches and this fails on the phrase it must not print.
    """
    confirmed, lines = what_the_hunt_established("Memo1", seen=0, read=0, unread=0, throttled=20)
    text = " ".join(lines)
    assert confirmed is False
    assert "NOT ESTABLISHED" in text
    assert "neither evidence it is right" in text
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


def test_the_four_outcomes_are_distinguishable_from_each_other():
    """Rule 14: "did nothing" must not look like "did work". Asserted on the OUTPUT, since
    the output is what the operator reads.

    FOUR NOW, NOT THREE. The zero-read case split on 2026-09-30 into "the endpoint refused to
    answer" and "the endpoint answered and we could not use it", because the operator's run
    showed why: the first says re-run smaller or pay for an endpoint, the second is a finding
    about chains/solana.py. One sentence for both sent the reader to the wrong place half
    the time.
    """
    texts = [
        " ".join(what_the_hunt_established("M", seen=1, read=1, unread=0)[1]),
        " ".join(what_the_hunt_established("M", seen=0, read=0, unread=0, throttled=40)[1]),
        " ".join(what_the_hunt_established("M", seen=0, read=0, unread=1, throttled=0)[1]),
        " ".join(what_the_hunt_established("M", seen=0, read=1, unread=0)[1]),
    ]
    assert len(set(texts)) == 4, "two outcomes render identically, which is the defect"


# ---------------------------------------------------------------------------
# THE THROTTLE, AND THE RUN THAT FORCED ALL OF IT.
#
# The operator ran `--hunt-memo 50` against api.devnet.solana.com on 2026-09-30. Measured
# from the output they pasted back:
#
#   MemoSq4gqABA...   10 read, 10 memos found, then 40 consecutive HTTP 429  -> CONFIRMED
#   Memo1UhkJRfH...   0 read, 22 consecutive HTTP 429, then Ctrl-C           -> nothing
#
# So the run DID settle the v2 program id, off ten of somebody else's memos, and settled
# nothing about v1. Three defects made that nearly unreadable and made them stop it:
#
#   1. a 429 was counted and reported as "could not be read", identically to a transaction
#      whose encoding the adapter cannot parse. Only the second is about our code.
#   2. no backoff and no give-up: forty refusals in a row were each asked for anyway, and
#      then twenty-two more against the second id.
#   3. six lines of error text per refusal -- 62 blocks -- with the one CONFIRMED line in
#      the middle of them.
#
# Rule 14 says silence is a defect. This is the same defect from the other side: output
# that says nothing a reader can act on, at volume.
# ---------------------------------------------------------------------------


def test_a_throttled_hunt_blames_the_endpoint_and_says_what_to_do_about_it():
    """The v1 outcome from the operator's run, seeded exactly: 22 asked, all refused.

    The old wording called those "unreadable transaction(s)" and then said, two lines later,
    that a 429 means the endpoint throttled us -- contradicting the count it had just given.
    """
    confirmed, lines = what_the_hunt_established("Memo1", seen=0, read=0, unread=0, throttled=22)
    text = " ".join(lines)
    assert confirmed is False
    assert "THE ENDPOINT, NOT THE ID" in text
    assert "22 read(s) were refused" in text
    assert "--hunt-memo 5" in text, "rule 14: the instruction has to be on the screen"
    assert "the id is wrong" not in text
    assert "unreadable" not in text, (
        "a rate limit is not an unreadable transaction, and calling it one is what made 40 "
        "refusals look like 40 bad transactions"
    )


def test_a_zero_read_with_no_throttle_points_at_our_own_code_instead():
    """The other half of the split, and it must NOT offer the re-run-smaller advice.

    Nothing was throttled, so a smaller N changes nothing; what came back is the problem, and
    an encoding or field-shape failure here is a finding about chains/solana.py.
    """
    confirmed, lines = what_the_hunt_established("Memo1", seen=0, read=0, unread=7, throttled=0)
    text = " ".join(lines)
    assert confirmed is False
    assert "none was a" in text and "rate limit" in text
    assert "chains/solana.py" in text
    assert "--hunt-memo 5" not in text, "a smaller N does not help when nothing was throttled"


def test_a_confirmed_id_stays_confirmed_even_when_most_reads_were_throttled():
    """THE OPERATOR'S ACTUAL v2 RESULT, and the one thing that must not regress.

    Ten reads, ten memos, forty throttles. The throttling is real and is worth printing, and it
    does not weaken what the ten reads proved: `memo_strings_in()` read real memo instructions
    under that program id. A verdict that downgraded to NOT ESTABLISHED because most reads
    failed would discard a measurement that was actually taken.
    """
    confirmed, lines = what_the_hunt_established("MemoSq4", seen=10, read=10, unread=0, throttled=40)
    text = " ".join(lines)
    assert confirmed is True
    assert "CONFIRMED" in text
    assert "MemoSq4" in text


def test_throttled_defaults_to_zero_so_the_old_call_shape_still_means_what_it_meant():
    """`throttled` is keyword-only with a default, and the default is the no-throttle case.

    Checked because a required argument here would have meant editing every existing call, and
    a default that silently routed old calls into the NEW branch would rewrite what those tests
    assert without touching them.
    """
    without = what_the_hunt_established("M", seen=0, read=0, unread=3)
    explicit = what_the_hunt_established("M", seen=0, read=0, unread=3, throttled=0)
    assert without == explicit


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


# ---------------------------------------------------------------------------
# THE RETRY AND THE GIVE-UP. What the operator's Ctrl-C was about.
# ---------------------------------------------------------------------------


class ScriptedAdapter:
    """Answers `getTransaction` from a scripted list, recording how many times it was asked.

    NO SLEEPING: the module's pacing and backoff constants are monkeypatched to zero by the
    fixture below, so these tests measure the CONTROL FLOW rather than the wall clock. What is
    being pinned is how many times the endpoint is asked and when the hunt stops asking --
    both of which the real delays only make slower to observe.
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = 0

    def call(self, method, *_params):
        assert method == "getTransaction", method
        self.asked += 1
        answer = self.answers.pop(0) if self.answers else _throttle()
        if isinstance(answer, Exception):
            raise answer
        return answer


def _throttle() -> SolanaRPCError:
    return SolanaRPCError("getTransaction returned HTTP 429 from devnet", status_code=429)


def _memo_transaction(text: str) -> dict:
    """The jsonParsed shape memo_strings_in() reads, minimal but real."""
    return {"transaction": {"message": {"instructions": [
        {"program": "spl-memo", "parsed": text,
         "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"},
    ]}}, "meta": {}}


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Zero every delay, so these run instantly and still exercise the real functions."""
    monkeypatch.setattr(solana_chain_check, "MEMO_HUNT_PACING_SECONDS", 0)
    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)


def test_one_throttled_read_is_retried_rather_than_counted_as_a_failure():
    """A 429 is the one status where asking again shortly is the right answer.

    Seeded to refuse once and then answer. The old code had no retry at all -- a single
    transient 429 permanently cost that transaction, which over 50 reads against a public
    endpoint is most of them.
    """
    adapter = ScriptedAdapter([_throttle(), _memo_transaction("4242")])
    transaction, throttled, reason = read_one_transaction(adapter, "sig")
    assert transaction is not None
    assert throttled is False
    assert reason == ""
    assert adapter.asked == 2, "asked once, refused, asked again"


def test_a_persistent_throttle_gives_up_and_says_it_was_a_throttle():
    """Bounded. The retry must not become the thing that hangs instead."""
    adapter = ScriptedAdapter([])  # every answer is a 429
    transaction, throttled, reason = read_one_transaction(adapter, "sig")
    assert transaction is None
    assert throttled is True
    assert "429" in reason
    assert adapter.asked == RPC_RETRIES_PER_CALL, (
        f"tried {adapter.asked} times; the per-read cap is {RPC_RETRIES_PER_CALL}"
    )


def test_a_non_throttle_failure_is_NOT_retried_and_is_NOT_called_a_throttle():
    """The split that the whole change is for.

    A 503, a bad encoding, a missing field -- none is fixed by waiting, and none says anything
    about the endpoint's willingness to answer. Retrying them would multiply the wait for no
    gain, and reporting them as throttles would hide a real finding about chains/solana.py.
    """
    # THE REAL MESSAGE SHAPE, formatted the way chains/solana.py's raise site formats it.
    # The first version of this seed said just "node is unwell" and the assertion below failed
    # -- correctly: a hand-written message that omits what the real one carries tests the test.
    adapter = ScriptedAdapter([SolanaRPCError(
        "getTransaction returned HTTP 503 from devnet: node is unwell", status_code=503)])
    transaction, throttled, reason = read_one_transaction(adapter, "sig")
    assert transaction is None
    assert throttled is False, "503 is not a rate limit"
    assert "503" in reason
    assert adapter.asked == 1, "a non-throttle must not be retried"


def test_the_hunt_stops_asking_after_three_refusals_in_a_row(capsys):
    """THE OPERATOR'S RUN, REPRODUCED AND BOUNDED.

    Fifty signatures, every read refused. The old loop asked all fifty, printed a six-line
    error block for each, and then started the next program id -- which is what the Ctrl-C
    interrupted. Three in a row ends it now, and the count of what was skipped is on screen.
    """
    adapter = ScriptedAdapter([])
    entries = [{"signature": f"sig{n}"} for n in range(50)]
    outcome = hunt_one_program_id(adapter, "MemoSq4", entries, 50)

    assert outcome.abandoned is True
    assert outcome.asked == MEMO_HUNT_GIVE_UP_AFTER_THROTTLES
    assert outcome.read == 0
    assert outcome.throttled == MEMO_HUNT_GIVE_UP_AFTER_THROTTLES
    assert outcome.unread == 0, "a throttle is not an unreadable transaction"
    assert outcome.of == 50

    out = capsys.readouterr().out
    assert "ABANDONED this id" in out
    assert "remaining 47" in out, "say how many were skipped, not just that it stopped"
    assert "says nothing about the program id" in out
    # ONE line for the refusal, not one per attempt. Counted, because burying the answer under
    # repeats is the defect this half is fixing.
    assert out.count("THROTTLED us") == 1, (
        f"printed the throttle notice {out.count('THROTTLED us')} times; the operator's run "
        f"printed 62 error blocks and the one real result was in the middle of them"
    )


def test_ten_good_reads_then_a_throttle_storm_still_confirms_the_id(capsys):
    """THE v2 RESULT FROM THE OPERATOR'S RUN, end to end through the real functions.

    Ten memos read, then nothing but refusals. The hunt must keep what it measured: the ten
    reads prove `memo_strings_in()` reads real memo instructions under that id, and abandoning
    the rest of the page does not unprove it.
    """
    adapter = ScriptedAdapter([_memo_transaction(f"{n}") for n in range(10)])
    entries = [{"signature": f"sig{n}"} for n in range(50)]
    outcome = hunt_one_program_id(adapter, "MemoSq4", entries, 50)

    assert outcome.read == 10
    assert outcome.seen == 10
    assert outcome.abandoned is True
    assert outcome.asked == 10 + MEMO_HUNT_GIVE_UP_AFTER_THROTTLES

    confirmed, lines = what_the_hunt_established(
        "MemoSq4", seen=outcome.seen, read=outcome.read,
        unread=outcome.unread, throttled=outcome.throttled)
    assert confirmed is True
    assert "CONFIRMED" in " ".join(lines)

    out = capsys.readouterr().out
    assert f"asked {outcome.asked} of 50" in out, "rule 3: the denominator, on the screen"
    assert "ABANDONED EARLY" in out, "rule 13: a partial run must not report like a full one"


def test_a_run_with_no_throttling_at_all_asks_for_everything_it_was_told_to(capsys):
    """The give-up must not fire on a healthy endpoint.

    MUTATION: count throttles cumulatively instead of consecutively, and a run with scattered
    refusals stops early for no reason. Seeded with a refusal between good reads to pin that
    the counter RESETS.
    """
    answers = [_memo_transaction("1"), _throttle(), _memo_transaction("2"),
               _memo_transaction("3"), _throttle(), _memo_transaction("4")]
    adapter = ScriptedAdapter(answers)
    entries = [{"signature": f"sig{n}"} for n in range(4)]
    outcome = hunt_one_program_id(adapter, "MemoSq4", entries, 4)

    assert outcome.abandoned is False, "two scattered 429s, each retried, is not a storm"
    assert outcome.asked == 4
    assert outcome.read == 4
    assert outcome.seen == 4
    assert outcome.throttled == 0, "a retry that then succeeded is not a throttled read"
    assert "ABANDONED" not in capsys.readouterr().out

def test_the_adapter_really_sets_the_status_from_a_real_429_response():
    """THE SEAM EVERY OTHER TEST HERE SKIPS, and a mutation walked straight through it.

    Every throttle test above constructs `SolanaRPCError(..., status_code=429)` by hand, so
    they all pass whether or not chains/solana.py's raise site actually passes the status
    through. Deleting `status_code=response.status_code` from that raise survived the entire
    suite -- which would mean `throttled` is False for every real rate limit, no retry ever
    happens, the give-up never fires, and the operator's screen goes back to what it was.

    So this drives the REAL `call()` with a stubbed HTTP response. `requests.post` is
    monkeypatched at the module the adapter imports it from; nothing opens a socket.
    """
    class Response:
        status_code = 429
        text = '{"jsonrpc":"2.0","error":{"code": 429, "message":"Too many requests"}}'

        def json(self):
            raise AssertionError("a non-200 must never be parsed as a result")

    adapter = solana_module.SolanaAdapter(url="http://127.0.0.1:1")
    original = solana_module.requests.post
    solana_module.requests.post = lambda *_a, **_k: Response()
    try:
        with pytest.raises(SolanaRPCError) as exc:
            adapter.call("getTransaction", "sig")
    finally:
        solana_module.requests.post = original

    assert exc.value.status_code == 429, "the status must survive the raise, not just the message"
    assert exc.value.throttled is True
    assert "429" in str(exc.value), "and the human-readable message still says it"


def test_the_consecutive_counter_RESETS_on_a_good_read(capsys):
    """CUMULATIVE vs CONSECUTIVE, and a mutation survived because nothing seeded the reset.

    `test_a_run_with_no_throttling_at_all` looked like it covered this and does not: its
    scattered 429s are retried into success INSIDE read_one_transaction, so the outer counter
    never reaches 1 and the reset it was meant to exercise is never reached. Deleting
    `in_a_row = 0` passed.

    This seeds REPORTED throttles -- each one exhausting its retries -- either side of a good
    read: two, then a success, then two. Consecutive, that peaks at 2 and the hunt finishes.
    Cumulative, it is 4 and the hunt abandons a healthy endpoint three reads early.
    """
    r = RPC_RETRIES_PER_CALL
    answers = (
        [_throttle()] * r          # signature 1: reported throttle
        + [_throttle()] * r        # signature 2: reported throttle  (in_a_row == 2)
        + [_memo_transaction("7")]  # signature 3: a good read -- the reset
        + [_throttle()] * r        # signature 4: reported throttle  (in_a_row == 1 again)
        + [_throttle()] * r        # signature 5: reported throttle  (in_a_row == 2)
    )
    adapter = ScriptedAdapter(answers)
    entries = [{"signature": f"sig{n}"} for n in range(5)]
    outcome = hunt_one_program_id(adapter, "MemoSq4", entries, 5)

    assert outcome.abandoned is False, (
        "never three REPORTED throttles in a row, so the endpoint had not stopped answering"
    )
    assert outcome.asked == 5, "all five signatures were attempted"
    assert outcome.throttled == 4
    assert outcome.read == 1
    assert outcome.seen == 1
    assert "ABANDONED" not in capsys.readouterr().out

# ---------------------------------------------------------------------------
# THE SUMMARY MUST NOT CONTRADICT THE RUN IT IS SUMMARIZING.
#
# 2026-09-30, `--hunt-memo 50`: two CONFIRMED lines printed, one per program id, and four
# lines later the summary said "What is still unproven is the memo PROGRAM IDS, written from
# documentation and never seen on a cluster: re-run with --hunt-memo N to settle them." It
# told the operator to re-run the thing they had just run, to settle what it had just settled.
#
# The least excusable of the six stale-claim defects that day: hunt_memo() RETURNS whether an
# id was confirmed, its docstring says it returns that "so a caller can print the difference",
# and main() discarded the value -- in the same commit that added MEASURED_MEMO_PROGRAM_IDS so
# this banner could not drift. The per-id line at the TOP of the hunt derived; the one at the
# bottom was hand-written.
# ---------------------------------------------------------------------------


def test_the_summary_derives_the_memo_status_and_never_calls_a_measured_id_unproven():
    """MUTATION: hand-write "still unproven" back into the summary and this fails."""
    for hunted in (None, True, False):
        text = " ".join(memo_status_lines(hunted))
        assert "measured against a real cluster" in text
        assert "never seen on a cluster" not in text
        assert "still unproven" not in text.lower()

    # Derived from the set, so the COUNT moves when the set does rather than being spelled.
    measured = [p for p in MEMO_PROGRAM_IDS if p in MEASURED_MEMO_PROGRAM_IDS]
    assert f"{len(measured)} of {len(MEMO_PROGRAM_IDS)} measured" in " ".join(memo_status_lines(None))


def test_an_unmeasured_id_is_named_and_a_measured_one_is_not_nagged_about():
    """The instruction appears only where it is actionable.

    MUTATION: print "--hunt-memo N settles it" unconditionally and a fully-measured tree nags
    the operator to re-run a settled check, which is the defect this whole test group is about.
    """
    real = solana_chain_check.MEASURED_MEMO_PROGRAM_IDS
    try:
        solana_chain_check.MEASURED_MEMO_PROGRAM_IDS = frozenset()
        none_measured = " ".join(solana_chain_check.memo_status_lines(None))
        solana_chain_check.MEASURED_MEMO_PROGRAM_IDS = frozenset(MEMO_PROGRAM_IDS)
        all_measured = " ".join(solana_chain_check.memo_status_lines(None))
    finally:
        solana_chain_check.MEASURED_MEMO_PROGRAM_IDS = real

    assert "NOT YET MEASURED" in none_measured
    assert "Add --hunt-memo" in none_measured, "with nothing measured, say how to fix it"
    assert "NOT YET MEASURED" not in all_measured
    assert "--hunt-memo" not in all_measured, "do not send somebody to re-run a settled check"


def test_a_hunt_that_confirmed_nothing_reads_differently_from_no_hunt_at_all():
    """Rule 14: "did nothing" and "did work and found nothing" must not share a line."""
    assert memo_status_lines(None) != memo_status_lines(False)
    assert "confirmed NOTHING" in " ".join(memo_status_lines(False))
    assert "a throttled hunt is not a finding" in " ".join(memo_status_lines(False))


def test_the_summary_still_names_the_credit_path_as_the_unproven_half(capsys):
    """What a PASSED run must keep saying, because this is the part a run has not touched.

    getBalance, getAccountInfo and find_deposits_to_address have never met a real response --
    every run so far passed no --address and no --mint. A summary that said only "the read path
    works" would read as more than it is.
    """
    # read_address=False, which is what "unproven" now means -- the wording is DERIVED from
    # whether the run read an address rather than asserted unconditionally. It used to be a
    # fixed sentence, so the first run that DID pass an address would have been told its own
    # coverage did not happen.
    print_summary([], 1.0, hunted=True,
                  observed=CreditPathObserved(address_read=False, is_spl=False,
                                              signatures=0, credits=0, refused=0))
    out = capsys.readouterr().out
    assert "CREDIT path: NOT exercised" in out
    assert "find_deposits_to_address" in out

    # AND THE MEMO LINES IN THE SUMMARY ARE THE DERIVED ONES, byte for byte. Asserted here
    # because without it, replacing the loop with a hand-written "still unproven" sentence
    # survived the whole suite -- which is the exact defect, restored.
    for line in memo_status_lines(True):
        assert line in out, "print_summary must emit memo_status_lines(), not its own wording"
    assert "never seen on a cluster" not in out


def test_the_hunt_asks_for_a_LATER_transaction_version_than_the_credit_path():
    """A REAL MEMO WAS INVISIBLE TO THIS TREE, and the two sites differ for a stated reason.

    The operator's 2026-09-30 re-run: one of fifty v2 reads answered `-32015 Transaction
    version (1) is not supported by the requesting client`. Versioned transactions are ordinary
    on Solana, so that is a read lost every run.

    chains/solana.py's deposit reader stays at 0 because `_native_credits` maps an address to a
    balance index through `message.accountKeys` alone, and a versioned transaction can draw keys
    from an address lookup table that arrives in `meta.loadedAddresses` -- absent from
    accountKeys, so a real deposit is silently not credited. Sound, and about the CREDIT path.

    It does not reach the hunt, and that is checked rather than argued: chains/solana_memo.py
    never mentions accountKeys or loadedAddresses at all, so there is no index to misalign.
    Asserted below by reading that module, so the day somebody wires positional key handling
    into the memo parser, this stops being true and says so.
    """
    assert MEMO_HUNT_TRANSACTION_VERSION > 0

    memo_source = Path(chains_solana_memo.__file__).read_text(encoding="utf-8")
    for key in ("accountKeys", "loadedAddresses", "preBalances", "postBalances"):
        assert key not in memo_source, (
            f"the memo parser now reads {key}, so it inherits the positional-index hazard that "
            f"pins the credit path to maxSupportedTransactionVersion 0 -- re-read both comments"
        )

    # AND EACH SITE NAMES THE OTHER (rule 8: if they genuinely differ, say so at both).
    check = Path(solana_chain_check.__file__).read_text(encoding="utf-8")
    adapter = Path(chains_solana.__file__).read_text(encoding="utf-8")
    assert "chains/solana_memo.py never touches `accountKeys`" in adapter
    assert "MEMO_HUNT_TRANSACTION_VERSION" in adapter, "the adapter names the hunt's constant"
    assert "the credit path stays at 0" in check.lower() or "CREDIT path stays at 0" in check

class _WholeClusterStub:
    """Answers every RPC the check makes, with shapes taken from the operator's real output.

    NOT A NETWORK, and not a paraphrase of the adapter either: `main()` builds a real
    SolanaAdapter and this replaces `requests.post`, so the adapter's own transport, error
    handling and result unwrapping all run. Only the wire is fake.
    """

    def __init__(self, *, signatures=2):
        self.signatures = signatures
        self.methods = []

    def __call__(self, _url, data=None, **_kwargs):
        # `data=json.dumps(payload)`, NOT `json=payload` -- which is how chains/solana.py
        # actually posts (solana.py:418). The first version of this stub took a `json=` kwarg,
        # so every request arrived as None and the recorded bodies were a list of Nones. A stub
        # whose signature does not match the real call site records nothing and proves nothing.
        payload = _json.loads(data) if data else {}
        method = payload.get("method", "")
        self.methods.append(method)
        return _Ok(self._result(method))

    def _result(self, method):
        # THE ADDRESS METHODS ARE ANSWERED TOO, since 2026-09-30 -- before that the ADDRESS
        # section was skipped on a default run, so a stub that covered only the cluster was
        # enough. It no longer is, and that is the point of the change: the section that
        # exercises the credit path now always runs.
        return {
            "getHealth": "ok",
            "getVersion": {"solana-core": "4.3.0"},
            "getGenesisHash": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
            "getSlot": 506046940,
            "getEpochInfo": {"epoch": 1171, "slotIndex": 174942, "absoluteSlot": 506046942},
            "getMinimumBalanceForRentExemption": 650240,
            "getBalance": {"value": 2_000_000_000},
            "getSignaturesForAddress": [{"signature": f"sig{n}"} for n in range(self.signatures)],
            "getTransaction": _memo_transaction("4242"),
        }.get(method, {})


class _Ok:
    status_code = 200

    def __init__(self, result):
        self._result = result

    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": self._result}


def _run_main(monkeypatch, capsys, argv):
    """main() end to end over the stub, returning (exit code, printed output)."""
    _point_config_at_the_stub(monkeypatch)
    monkeypatch.setattr(chains_solana.requests, "post", _WholeClusterStub())
    monkeypatch.setattr("sys.argv", argv)
    code = solana_chain_check.main()
    return code, capsys.readouterr().out


def _point_config_at_the_stub(monkeypatch):
    """Only the url is overridden. EVERY OTHER KEY COMES FROM THE REAL Config.RPC["SOL"].

    The first version of this built the dict from scratch with url and commitment, and
    print_banner raised KeyError on 'mint' -- because main() passes that dict straight into
    SolanaAdapter(**rpc) and reads rpc['mint'] for the banner. A hand-built stub config tests
    the stub; spreading the real one means a key added to Config later arrives here too.
    """
    monkeypatch.setattr(
        solana_chain_check.Config, "RPC",
        {**solana_chain_check.Config.RPC,
         "SOL": {**solana_chain_check.Config.RPC["SOL"], "url": "http://127.0.0.1:1"}},
    )


def test_main_runs_without_a_hunt_and_does_not_crash_at_the_summary(monkeypatch, capsys):
    """THE PLAIN RUN, WHICH IS THE ONE THE OPERATOR USES MOST, AND IT WAS A NameError.

    `hunted` was assigned only inside `if args.hunt_memo > 0`, so every run without the flag
    would have raised NameError at print_summary. ruff does not flag a conditionally-bound
    local, the whole suite passed, and deleting the initializer survived every mutation check
    until this test existed -- because nothing called main() at all.

    That is the shape worth remembering: three mutations in main() survived together, and the
    reason was not that the assertions were weak. It was that the function had no test.
    """
    code, out = _run_main(monkeypatch, capsys, ["solana_chain_check.py"])
    assert code == 0
    assert "SUMMARY" in out
    assert "PASSED" in out
    assert "measured against a real cluster" in out, "the derived memo line, on a plain run"
    assert "MEMO PROGRAM" not in out, "no hunt was asked for, so none ran"
    assert "no hunt ran this time" not in out, (
        "both ids are measured, so there is nothing for a hunt to settle and nothing to nag about"
    )


def test_main_with_a_hunt_feeds_the_result_into_the_summary(monkeypatch, capsys):
    """THE RETURN VALUE REACHES THE SUMMARY, which is what discarding it broke.

    MUTATION: drop `hunted` from the print_summary() call and this fails -- with every id
    measured the wording is the same either way, so the assertion is made against a tree where
    NOTHING is measured, which is the state that makes the two paths differ.
    """
    monkeypatch.setattr(solana_chain_check, "MEASURED_MEMO_PROGRAM_IDS", frozenset())
    code, out = _run_main(monkeypatch, capsys, ["solana_chain_check.py", "--hunt-memo", "2"])

    assert code == 0, "the hunt is outside the exit code -- a quiet cluster is not a bad adapter"
    assert "MEMO PROGRAM" in out
    assert "CONFIRMED" in out, "the stub serves a real memo, so the hunt confirms"
    assert "0 of 2 measured" in out, "the constant says none; the summary reports the constant"
    assert "no hunt ran this time" not in out, (
        "a hunt DID run. That line is only for a run that skipped it -- and it appears here iff "
        "print_summary was handed None instead of the hunt's result"
    )


def test_main_asks_for_the_transaction_version_the_hunt_declares(monkeypatch, capsys):
    """END TO END: the constant reaches the wire, not just the module.

    MUTATION: hardcode 0 at the call site inside read_one_transaction and the constant becomes
    decoration. Checked on the recorded request body rather than on the source.
    """
    bodies = []

    class Recording(_WholeClusterStub):
        def __call__(self, url, data=None, **kwargs):
            bodies.append(_json.loads(data) if data else {})
            return super().__call__(url, data=data, **kwargs)

    _point_config_at_the_stub(monkeypatch)
    monkeypatch.setattr(chains_solana.requests, "post", Recording())
    monkeypatch.setattr("sys.argv", ["solana_chain_check.py", "--hunt-memo", "1"])
    solana_chain_check.main()
    capsys.readouterr()

    reads = [b for b in bodies if b.get("method") == "getTransaction"]
    assert reads, "the hunt must actually call getTransaction"

    # TWO CALL SITES NOW RUN IN ONE INVOCATION, and they ask for DIFFERENT versions on purpose.
    # This assertion used to require every getTransaction to carry the hunt's version, which was
    # true only while the ADDRESS section was skipped by default. Now find_deposits_to_address
    # runs too and asks for version 0 -- because `_native_credits` indexes positionally through
    # message.accountKeys and a lookup table's keys arrive elsewhere, which would silently miss a
    # deposit. The hunt asks for 1 because chains/solana_memo.py reads no keys at all. Pinning
    # BOTH is stronger than pinning either: it holds the split end to end, at the wire.
    # ONE ASSERTION, NOT FOUR, and it is the stronger one. A first draft read the version and the
    # encoding out of each config by subscript and asserted on them separately -- which put that
    # field's name in a position where tests/test_address_literals_are_valid.py reads it as an
    # address-shaped literal (thirty alphanumerics, no separator) and failed the run. The
    # whole-config comparison below pins both versions AND both encodings AND the absence of any
    # third key, with every field name in KEY position where the scanner exempts it. Weaker
    # assertions were what needed the subscripts.

    # THE WHOLE CONFIG, COMPARED AS A DICT LITERAL, and the shape of this assertion is not a
    # style choice. Reading the version field out by subscript puts its name in a position where
    # tests/test_address_literals_are_valid.py's scanner treats it as an address-shaped literal
    # -- thirty alphanumerics with no separator, which is what a base58 address looks like -- and
    # that gate has already been widened once this session to accommodate a call site, then
    # reverted, because loosening an address check to quiet a false positive is how a real
    # malformed address gets through later. A DICT KEY position is exempt by design, so the
    # expected config is written as a literal and compared whole.
    #
    # (This comment was itself the second offender: the scanner reads comment text too, so
    # spelling the subscript form here to explain the rule broke the rule. Reworded rather than
    # exempted -- four checks in this suite have now tripped on their own subject matter, and
    # every time the cheap fix was to stop naming it, not to stop checking for it.)
    #
    # Comparing the whole dict is also the stronger assertion: a key ADDED to the request would
    # slip past two field checks and is caught here.
    # Each config is EXACTLY one of the two expected dicts and carries no third key. Written as
    # dict literals so the version field's name stays in KEY position -- see the note above.
    # The deposit reader also pins a COMMITMENT and the hunt does not, which is right: the
    # deposit reader decides whether money is credited and must read at a stated rung of the
    # ladder, while the hunt only wants to see somebody's memo text. DISCOVERY_COMMITMENT is
    # imported rather than spelled "processed" here, so the two cannot drift.
    assert {frozenset(body["params"][1].items()) for body in reads} == {
        frozenset({"encoding": "jsonParsed",
                   "maxSupportedTransactionVersion": MEMO_HUNT_TRANSACTION_VERSION}.items()),
        frozenset({"encoding": "jsonParsed",
                   "commitment": DISCOVERY_COMMITMENT,
                   "maxSupportedTransactionVersion": 0}.items()),
    }, "a key was added to, or removed from, one of the two getTransaction configs"

# ---------------------------------------------------------------------------
# THE CHECK HAS TO BE RUNNABLE WITH NO ARGUMENTS.
#
# 2026-09-30. The credit path is the half of this adapter that loses money if a
# field name is wrong, and it is exercised only by the ADDRESS section -- which
# was skipped entirely unless the operator supplied an address. I then handed
# them one as a placeholder inside a code block, and they pasted it:
#
#     python3 solana_chain_check.py --address <a devnet wallet with a balance>
#     bash: syntax error near unexpected token `newline'
#
# Two defects, mine both. A placeholder in a code block, which they had already
# told me never to do. And a check whose most valuable section required a value
# the repository had held in fund_testnets.py since it was written.
# ---------------------------------------------------------------------------


def test_an_explicit_address_always_wins():
    assert resolve_address("rTYPED", "rCONFIGURED")[0] == "rTYPED"
    assert "--address" in resolve_address("rTYPED", "rCONFIGURED")[1]


def test_the_configured_hot_wallet_is_used_when_nothing_was_typed():
    address, why = resolve_address("", "rCONFIGURED")
    assert address == "rCONFIGURED"
    assert "SOL_HOT_WALLET" in why


def test_WITH_NEITHER_IT_STILL_HAS_AN_ADDRESS_TO_READ():
    """THE FIX. A check that needs an argument is a check that does not get run.

    MUTATION: return ("", ...) here and the ADDRESS section goes back to being skipped on every
    default run, which is how the credit path stayed unexercised for five days.
    """
    address, _why = resolve_address("", "")
    assert address == SOLANA_DEVNET_ACCOUNT
    assert address, "the whole point: there is always something to read"
    assert is_valid_address(address), "and it is a real Solana address, not a placeholder"


def test_the_default_says_it_is_a_default_and_what_it_does_NOT_prove():
    """A default that appears silently is a default an operator reads as their own config.

    That distinction is load-bearing here: getBalance against the fallback proves the READ PATH
    and proves nothing about SOL_HOT_WALLET being set correctly. Rule 14 -- echo the parameter
    that decides the answer, and say what the answer means.
    """
    _, why = resolve_address("", "")
    assert "DEFAULTED" in why
    assert "SOL_HOT_WALLET" in why, "name the thing that was not set"
    assert "says nothing about your own wallet" in why


def test_the_default_address_is_not_a_placeholder_anybody_could_paste_wrong():
    """WHAT THE OPERATOR'S SHELL ERROR WAS, PINNED SO IT CANNOT COME BACK AS A CONSTANT.

    MUTATION: set SOLANA_DEVNET_ACCOUNT to "<a devnet wallet with a balance>" or any other
    human-readable stand-in and this fails. A constant that is not a real address is a
    placeholder with extra steps -- it would reach the cluster and come back actNotFound, which
    reads as "the account is empty" rather than "somebody left a note here".
    """
    assert is_valid_address(SOLANA_DEVNET_ACCOUNT)
    for shell_metacharacter in "<>|&;$(){}[] ":
        assert shell_metacharacter not in SOLANA_DEVNET_ACCOUNT, (
            f"{shell_metacharacter!r} in an address a human is meant to paste into a shell"
        )


def test_the_banner_prints_where_the_address_came_from(capsys):
    """MUTATION: drop address_why from the print_banner() call and the default goes silent."""
    # THE REAL Config.RPC["SOL"] SPREAD, not a hand-built dict. Built one by hand earlier in
    # this file and print_banner raised KeyError on 'mint'; it raised again here on
    # 'min_commitment_rank'. A hand-built config tests the hand-built config.
    rpc = {**solana_chain_check.Config.RPC["SOL"], "url": "http://127.0.0.1:1"}
    address, why = resolve_address("", "")
    print_banner(rpc, address, why)
    out = capsys.readouterr().out
    assert address in out
    assert "DEFAULTED" in out, "the banner must say the address was not the operator's choice"


# --- what the run actually covered -----------------------------------------


#: The operator's runs, as observations. Both are real: the first is the native run where a
#: credit was decoded and then refused for having no memo; the second is the --mint run where
#: _spl_credits' filter matched nothing over eight signatures.
NATIVE_DECODED = CreditPathObserved(address_read=True, is_spl=False, signatures=1,
                                    credits=0, refused=1)
SPL_FILTER_ONLY = CreditPathObserved(address_read=True, is_spl=True, signatures=8,
                                     credits=0, refused=0)
NOTHING_READ = CreditPathObserved(address_read=False, is_spl=False, signatures=0,
                                  credits=0, refused=0)


def test_with_no_address_the_summary_says_the_credit_path_did_not_run():
    text = " ".join(credit_path_lines(NOTHING_READ))
    assert "NOT exercised" in text
    assert "loses a deposit" in text


def test_a_READER_WHOSE_FILTER_MATCHED_NOTHING_IS_NOT_REPORTED_AS_EXERCISED():
    """THE FOURTH FALSE COVERAGE CLAIM, AND THE ONE THAT FORCED THE INTERFACE CHANGE.

    The operator's --mint run: eight signatures read, no credits, no drops. The summary said
    "both credit readers" had been exercised. They had not. `_spl_credits` selects token
    balances by owner AND mint before touching an amount, and the account's associated token
    account does not exist -- so the filter matched nothing, the loop body never ran, and
    `entry["uiTokenAmount"]["decimals"]` and `["amount"]` were never read. Those are exactly the
    field names that would lose an SPL deposit silently, which is the risk this whole script
    exists to retire.

    "The reader ran" and "the reader decoded a real amount" are different claims. Only the
    second retires anything.

    MUTATION: report coverage from whether a mint was passed and this fails.
    """
    text = " ".join(credit_path_lines(SPL_FILTER_ONLY))
    assert "PARTLY exercised" in text
    assert "matched nothing" in text
    assert "WITHOUT decoding" in text
    assert "still unproven" in text
    assert "DECODED" not in text, "nothing was decoded; saying so is the defect"
    assert "8 FETCHED signature(s)" in text, (
        "rule 3: the denominator it filtered over, and FETCHED rather than listed -- see "
        "test_an_unfetched_signature_is_NOT_counted_as_filtered_over"
    )


def test_a_reader_that_decoded_an_amount_says_so_and_names_the_other_one():
    """A REFUSED credit counts as decoded, and that distinction is not a technicality.

    The native run decoded the balance delta and THEN the memo check declined it -- so every
    field name in `_native_credits` had to be right to get that far. A reader that never
    produced anything cannot make that claim.
    """
    text = " ".join(credit_path_lines(NATIVE_DECODED))
    assert "_native_credits DECODED" in text
    assert "1 refused" in text
    assert "_spl_credits (needs --mint)" in text, "name the reader still outstanding, and how"
    assert "PARTLY" not in text


def test_zero_signatures_says_the_reader_was_never_invoked_at_all():
    """Three ways to end with no credits, and they are not the same fact.

    Nothing read, filter matched nothing, and credits decoded each call for a different next
    action -- point the address somewhere with activity, point it at an account that received
    the asset, or nothing.
    """
    text = " ".join(credit_path_lines(
        CreditPathObserved(address_read=True, is_spl=True, signatures=0, credits=0, refused=0)))
    assert "ZERO signatures" in text
    assert "never invoked at all" in text
    assert "Nothing about the readers was established" in text


def test_all_five_observed_states_are_distinguishable():
    """MUTATION: collapse any two and this fails. Five states, four different next actions."""
    states = [
        NOTHING_READ,
        CreditPathObserved(True, True, 0, 0, 0),
        SPL_FILTER_ONLY,
        NATIVE_DECODED,
        CreditPathObserved(True, True, 8, 2, 0),
    ]
    assert len({" ".join(credit_path_lines(o)) for o in states}) == 5


def test_decoded_an_amount_counts_a_REFUSAL_and_not_only_a_CREDIT():
    """The property the wording rests on, asserted directly.

    MUTATION: define decoded_an_amount as `bool(self.credits)` and the native run -- which
    decoded one amount and refused it -- reports as only partly exercised, understating what was
    actually proven.
    """
    assert NATIVE_DECODED.decoded_an_amount is True, "a refusal means the amount was decoded"
    assert CreditPathObserved(True, True, 8, 2, 0).decoded_an_amount is True
    assert SPL_FILTER_ONLY.decoded_an_amount is False
    assert NOTHING_READ.decoded_an_amount is False


def test_the_reader_is_named_from_the_adapters_mode_and_not_from_a_string():
    assert SPL_FILTER_ONLY.reader == "_spl_credits"
    assert NATIVE_DECODED.reader == "_native_credits"


def test_main_with_no_arguments_reads_an_address_and_says_the_credit_path_ran(monkeypatch, capsys):
    """END TO END: the default reaches the wire and the summary reports it.

    This is the run the operator could not make. No flags, no environment, and the ADDRESS
    section executes -- which is what makes the credit-path claim in the summary true.
    """
    code, out = _run_main(monkeypatch, capsys, ["solana_chain_check.py"])
    assert code == 0
    assert SOLANA_DEVNET_ACCOUNT in out
    assert "DEFAULTED" in out
    assert "ADDRESS" in out
    assert "(none given" not in out, "the section that was skipped for five days now runs"
    assert "CREDIT path: NOT exercised" not in out, "an address WAS read"

    # PARTLY, AND THAT IS CORRECT FOR THIS STUB. Its getTransaction credits the memo program's
    # own account rather than the address under test, so `_native_credits` finds
    # `address not in keys` and returns nothing -- the filter ran and no amount was decoded.
    # The summary saying so is the interface change working end to end: it reports what the run
    # DID, not that --address was passed.
    assert "PARTLY exercised" in text_of(out)
    assert "WITHOUT decoding" in text_of(out)
    assert "DECODED" not in out


def test_main_reports_DECODED_when_the_run_actually_credits_the_address(monkeypatch, capsys):
    """THE OTHER SIDE OF THE SAME INTERFACE, so "partly" is not the only thing it can say.

    Seeded so the transaction really credits the address under test and carries a memo in the
    allocator's range -- which is a swap deposit, the case the whole path exists for.

    MUTATION: report coverage from the flags again and both this test and the one above pass
    with the same sentence, which is how three false claims shipped.
    """
    address = SOLANA_DEVNET_ACCOUNT
    crediting = {
        "transaction": {"message": {
            "accountKeys": [address],
            "instructions": [{"program": "spl-memo", "parsed": "4242",
                              "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"}],
        }},
        "meta": {"preBalances": [0], "postBalances": [2_000_000], "err": None},
    }

    class Crediting(_WholeClusterStub):
        def _result(self, method):
            if method == "getTransaction":
                return crediting
            return super()._result(method)

    _point_config_at_the_stub(monkeypatch)
    monkeypatch.setattr(chains_solana.requests, "post", Crediting(signatures=1))
    monkeypatch.setattr("sys.argv", ["solana_chain_check.py"])
    code = solana_chain_check.main()
    out = capsys.readouterr().out

    assert code == 0
    assert "_native_credits DECODED" in text_of(out)
    assert "1 credited" in text_of(out)
    assert "PARTLY" not in out
    assert "vout=4242" in out, "the memo tag became the discriminator, on a real-shaped response"


def text_of(out: str) -> str:
    """The output with line breaks collapsed, for asserting on sentences rather than layout.

    Same reason as tests/test_solana_memo.py's _unwrapped(): two assertions in this suite have
    already failed on a line wrap rather than on the wording, and the credit-path lines are
    wrapped deliberately to fit a terminal.
    """
    return " ".join(out.split())

# ---------------------------------------------------------------------------
# "(none)" MUST NOT COVER A STRANDED DEPOSIT. The operator's 2026-09-30 run:
#
#   SOL deposit 2K2Pw1Hz... CANNOT BE ATTRIBUTED and was NOT credited ... 1 credit(s) dropped
#       ok   (none)  <- zero credits in the signatures read. This is a RESULT, not a failure.
#
# One credit was read and refused. "zero credits in the signatures read" was
# false, in the line an operator reads, four lines under the log that said so.
# ---------------------------------------------------------------------------


class _DepositStub:
    """Stands in for the adapter, exposing exactly what _deposits_line() reads."""

    def __init__(self, events=(), drops=(), signatures_read=0, unreadable=()):
        self._events = list(events)
        self.unattributable_drops = list(drops)
        self.signatures_read = signatures_read
        # THE STUB HAS TO CARRY EVERY ATTRIBUTE THE REAL ADAPTER EXPOSES, and this one was
        # added on 2026-10-01 -- a stub missing a new attribute fails loudly here, which is the
        # cheap outcome. The expensive one would be a stub that silently returns a default and
        # lets the test agree with a shape the adapter no longer has.
        self.unreadable_signatures = list(unreadable)
        self.signatures_listed = signatures_read + len(self.unreadable_signatures)
        self.min_commitment_rank = 3

    def find_deposits_to_address(self, _address, tx_limit=10):
        return self._events


def _a_drop(credits=1):
    return chains_solana.UnattributableCredit(
        signature="2K2Pw1Hz", credits=credits,
        why="no memo instruction -- unattributable, and a human has to match it")


def test_a_refused_credit_is_NEVER_reported_as_zero_credits_read():
    """THE FALSE LINE, PINNED.

    MUTATION: restore the single `if not events: return "(none) ... zero credits"` branch and
    this fails -- which is the sentence that printed over somebody's stranded deposit.
    """
    line = _deposits_line(_DepositStub(drops=[_a_drop()], signatures_read=1), "rADDR", 10)
    assert "zero credits" not in line
    assert "WERE READ AND REFUSED" in line
    assert "1 credit(s)" in line
    assert "2K2Pw1Hz" in line, "the signature, because a human has to go and find it"
    assert "no memo instruction" in line, "and why it was refused"
    assert "human's job" in line


def test_the_adapters_WARNING_is_folded_INTO_the_step_and_not_left_to_cross_it():
    """THE FRAME HOLDS. Nothing this step emits starts at column 0.

    On the operator's 2026-09-30 run the adapter's WARNING went straight to stderr the moment it
    happened, so an unindented 300-character sentence landed between the step's announcement and
    its result. CLAUDE.md's "every diagnostic has to be a single pasteable block" is not a style
    note there: the operator pastes this back, and a line at column 0 mid-step breaks the
    alignment that makes the block readable.

    THE FIRST FIX FOR THIS WAS A NOTE, NOT A FIX. The stranded line ended with "(the WARNING
    above this step is the same event, logged; it is not a second one)" -- prose explaining a
    formatting defect rather than removing it. The record is buffered and re-emitted indented
    now, so there is nothing above the step to explain.

    MUTATION: drop the handler and let the record propagate, and this fails -- driven through a
    real logger rather than a stub, so the capture is exercised and not described.
    """
    address = "J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM"
    memoless = {
        "transaction": {"message": {"accountKeys": [address], "instructions": []}},
        "meta": {"preBalances": [0], "postBalances": [5_000], "err": None},
    }
    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": "2K2Pw1Hz", "confirmationStatus": "confirmed"}],
        "getTransaction": memoless,
    })
    line = _deposits_line(adapter, address, 10)

    assert "logged:" in line, "the adapter's own record is still reported, not silenced"
    assert "CANNOT BE ATTRIBUTED" in line, "and it is the real record, not a paraphrase"
    assert "not a second one" not in line, "the caveat explained a defect that is now fixed"
    for continuation in line.splitlines()[1:]:
        assert continuation.startswith("      "), (
            f"a line in this step starts at column {len(continuation) - len(continuation.lstrip())}"
            f": {continuation[:60]!r}. The block has to survive being pasted."
        )


def test_the_capture_is_removed_even_when_the_call_raises():
    """A handler left attached would swallow every later warning in the process.

    MUTATION: drop the try/finally and this fails -- and the damage would be invisible, because
    the symptom is a warning that never appears rather than an error.
    """
    address = "J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM"
    adapter_logger = logging.getLogger("chains.solana")
    before = list(adapter_logger.handlers), adapter_logger.propagate

    exploding = _seeded_adapter({"getSignaturesForAddress": _raise})
    with pytest.raises(RuntimeError):
        _deposits_line(exploding, address, 10)

    assert list(adapter_logger.handlers) == before[0], "a handler survived the exception"
    assert adapter_logger.propagate is before[1], "propagation was left switched off"


def _raise(*_args):
    raise RuntimeError("the cluster went away mid-poll")


def _seeded_adapter(responses):
    """A real SolanaAdapter with only its transport replaced, so its logger really fires."""
    adapter = chains_solana.SolanaAdapter(url="http://seeded.invalid")

    def fake_call(method, *params):
        if method not in responses:
            raise AssertionError(f"the adapter called {method}, which this test did not seed")
        value = responses[method]
        return value(*params) if callable(value) else value

    adapter.call = fake_call
    return adapter


def test_several_drops_are_summed_across_transactions():
    """Two transactions, three credits: the operator needs both numbers."""
    line = _deposits_line(
        _DepositStub(drops=[_a_drop(credits=2), _a_drop(credits=1)], signatures_read=5), "rADDR", 10)
    assert "3 credit(s) across 2 transaction(s)" in line


def test_a_quiet_account_and_an_uncrediting_one_read_differently():
    """Rule 3's denominator, and rule 14's "did nothing must not look like did work".

    Zero signatures means nothing has touched the account. Signatures with no credits means the
    poll IS seeing traffic, which is what answers "is the watcher even running".
    """
    quiet = _deposits_line(_DepositStub(signatures_read=0), "rADDR", 10)
    busy = _deposits_line(_DepositStub(signatures_read=10), "rADDR", 10)

    assert "ZERO signatures" in quiet
    assert "10 signature(s) FETCHED and none credited" in busy, (
        "FETCHED, not read: the word changed on 2026-10-01 because the count did -- it is "
        "listed minus unreadable now, not the length of the signature list"
    )
    assert quiet != busy
    for line in (quiet, busy):
        assert line.startswith("(none)")
        assert "RESULT, not a failure" in line, "an empty result is still a result"


def test_all_four_deposit_outcomes_are_distinguishable():
    """MUTATION: collapse any two branches and this fails.

    Four, and only one of them is a problem -- which is why they cannot share a line.
    """
    event = {"txid": "sigOK", "vout": 4242, "amount": 1.5, "confirmations": 3}
    lines = {
        _deposits_line(_DepositStub(signatures_read=0), "rADDR", 10),
        _deposits_line(_DepositStub(signatures_read=10), "rADDR", 10),
        _deposits_line(_DepositStub(drops=[_a_drop()], signatures_read=1), "rADDR", 10),
        _deposits_line(_DepositStub(events=[event], signatures_read=1), "rADDR", 10),
    }
    assert len(lines) == 4


def test_a_real_credit_still_renders_its_credits():
    """The branch that was already right, so the new ones did not displace it."""
    event = {"txid": "sigOK", "vout": 4242, "amount": 1.5, "confirmations": 3}
    line = _deposits_line(_DepositStub(events=[event], signatures_read=1), "rADDR", 10)
    assert "1 credit(s):" in line
    assert "sigOK" in line
    assert "vout=4242" in line
    assert "REFUSED" not in line

def test_the_native_line_does_not_claim_a_call_the_native_path_never_makes():
    """getAccountInfo IS SPL-ONLY, and the native line named it until 2026-09-30.

    Measured by driving the adapter with a captured transport: a native run sends getBalance,
    getSignaturesForAddress and getTransaction. getAccountInfo is the owner-program read that
    decides token-program detection, and the mint-decimals read -- neither happens without a
    mint.

    The line no longer enumerates methods at all, which is the deeper fix: it names the READER
    and what that reader did, because a method list is a claim about plumbing while the reader is
    where a wrong field name costs a deposit. This test holds the specific regression anyway,
    since the method name reappearing would be the same mistake in a new sentence.
    """
    native = " ".join(credit_path_lines(NATIVE_DECODED))
    assert "getAccountInfo" not in native, (
        "getAccountInfo is never called without a mint, so the native line must not name it"
    )
    assert "_native_credits" in native, "name the reader, which is what was actually exercised"


# ---------------------------------------------------------------------------
# --find-holder: THE LAST UNPROVEN READER, WITHOUT A HUMAN HUNTING FOR AN ADDRESS.
#
# The operator's --mint run ended "PARTLY exercised ... WITHOUT decoding an
# amount", and the only next step was for somebody to find a wallet holding
# wrapped SOL. The cluster can answer that: getTokenLargestAccounts names the
# biggest token accounts for a mint, getAccountInfo on one names its owner.
#
# THE FIELD NAMES ARE FROM DOCUMENTATION AND UNMEASURED. api.devnet.solana.com
# still answers 403 through this container's proxy -- re-checked 2026-09-30, not
# assumed from the 2026-09-25 measurement. So these tests pin the REFUSALS as
# hard as the happy path: a wrong field name has to make the helper say so.
# ---------------------------------------------------------------------------

_A_HOLDER = "J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM"
# NAMED WITHOUT THE WORD "token" ON PURPOSE. As `_A_HOLDING_ACCOUNT` this tripped ruff's S105
# ("possible hardcoded password"), because the rule matches a name containing `token` assigned a
# string literal. It is a public base58 address and a `noqa` would have been defensible -- but
# rule 19 says a suppression is a claim you checked, and there is nothing to check here: the
# name was just unlucky. Renaming removes the finding instead of asserting past it.
_A_HOLDING_ACCOUNT = "FDhqCrFJki8JAg8qo9hzFoPuPUpBth4fTzgSjiZR2Ujp"
_A_MINT = "So11111111111111111111111111111111111111112"


def test_find_a_holder_returns_the_WALLET_and_not_the_token_account():
    """THE DISTINCTION THAT MAKES THIS WORK AT ALL.

    getTokenLargestAccounts names token ACCOUNTS. find_deposits_to_address matches on
    `owner == address` in the transaction's token balances, so handing it a token account would
    match nothing -- the same "PARTLY exercised" dead end, reached by a longer route. The owner
    comes from getAccountInfo's parsed data.
    """
    adapter = _seeded_adapter({
        "getTokenLargestAccounts": {"value": [{"address": _A_HOLDING_ACCOUNT,
                                               "uiAmountString": "12.5"}]},
        "getAccountInfo": {"value": {"data": {"parsed": {"info": {"owner": _A_HOLDER}}}}},
    })
    found, why = find_a_holder(adapter, _A_MINT)
    assert found == _A_HOLDER
    assert found != _A_HOLDING_ACCOUNT, "a token account is not a wallet"
    assert _A_HOLDING_ACCOUNT in why, "say which holding account led there"
    assert "12.5" in why, "and how much it holds, so the reader has something to decode"
    assert "nothing sent" in why


def test_a_mint_nobody_holds_says_so_rather_than_returning_an_address():
    """An empty value list is an answer about the mint, and it is not an error."""
    adapter = _seeded_adapter({"getTokenLargestAccounts": {"value": []}})
    found, why = find_a_holder(adapter, _A_MINT)
    assert found == ""
    assert "no holders" in why
    assert _A_MINT in why


def test_an_UNREADABLE_owner_is_reported_as_a_RESPONSE_SHAPE_finding():
    """THE REFUSAL THAT MATTERS, because these field names were never measured.

    If `data.parsed.info.owner` is not where the owner lives on a real cluster, the helper must
    say that it could not read it -- naming the path it looked under -- rather than returning
    nothing that reads like "no holders". The two are different findings and only one of them is
    about our code.

    MUTATION: return ("", "no holders") for this case and it becomes indistinguishable from an
    unheld mint, which is how a wrong field name would hide as a fact about the chain.
    """
    adapter = _seeded_adapter({
        "getTokenLargestAccounts": {"value": [{"address": _A_HOLDING_ACCOUNT}]},
        "getAccountInfo": {"value": {"data": {"parsed": {"info": {"authority": _A_HOLDER}}}}},
    })
    found, why = find_a_holder(adapter, _A_MINT)
    assert found == ""
    assert "data.parsed.info" in why, "name the path it looked under"
    assert "written from documentation and never measured" in why
    assert "not about the mint" in why
    assert "no holders" not in why


def test_it_walks_PAST_a_holder_whose_owner_cannot_be_read():
    """One unreadable entry must not abandon the lookup.

    MUTATION: return on the first entry and a single malformed row hides every good one behind
    it -- which over a mint with many holders is the difference between working and not.
    """
    def account_info(holding_account, _config):
        if holding_account == "UNREADABLE":
            return {"value": {"data": {"parsed": {"info": {}}}}}
        return {"value": {"data": {"parsed": {"info": {"owner": _A_HOLDER}}}}}

    adapter = _seeded_adapter({
        "getTokenLargestAccounts": {"value": [{"address": "UNREADABLE"},
                                              {"address": _A_HOLDING_ACCOUNT}]},
        "getAccountInfo": account_info,
    })
    found, _why = find_a_holder(adapter, _A_MINT)
    assert found == _A_HOLDER


def test_an_owner_that_is_not_a_valid_address_is_refused():
    """is_valid_address on the way out, because the next thing done with it is an RPC call.

    A garbage owner would be passed to find_deposits_to_address, which raises SolanaAddressError
    -- readable, but it would report as a failure of the deposit reader rather than of this
    lookup. Refusing here keeps the finding where it belongs.
    """
    adapter = _seeded_adapter({
        "getTokenLargestAccounts": {"value": [{"address": _A_HOLDING_ACCOUNT}]},
        "getAccountInfo": {"value": {"data": {"parsed": {"info": {"owner": "not-an-address"}}}}},
    })
    found, why = find_a_holder(adapter, _A_MINT)
    assert found == ""
    assert "readable `owner`" in why


def test_find_holder_without_a_mint_refuses_and_exits_nonzero(monkeypatch, capsys):
    """Native SOL has no holders to look up, and the flag says which flag it needs.

    MUTATION: fall through to the default address and the run would report coverage of a check
    the operator did not ask for, having silently ignored the flag.
    """
    _point_config_at_the_stub(monkeypatch)
    monkeypatch.setattr(chains_solana.requests, "post", _WholeClusterStub())
    monkeypatch.setattr("sys.argv", ["solana_chain_check.py", "--find-holder"])
    code = solana_chain_check.main()
    out = capsys.readouterr().out

    assert code == 1
    assert "--find-holder needs --mint" in out
    assert "ADDRESS" not in out, "it must not go on and check something else instead"

# ---------------------------------------------------------------------------
# A THROTTLE IS NOT A FINDING -- the same rule, in the second place it was needed.
#
# The operator's --find-holder run, 2026-09-30:
#
#   that lookup FAILED: SolanaRPCError: getTokenLargestAccounts returned HTTP 429
#   the field names in find_a_holder() were written from documentation and never
#   measured -- this is the finding, not a crash.
#
# Both lines wrong together. It did not retry a rate limit that retrying fixes,
# and then it blamed unmeasured field names for the endpoint refusing to answer.
# "A throttled hunt is not a finding" had been fixed in the memo hunt hours
# earlier and was welded to getTransaction, so the new helper inherited none of
# it -- rule 8 with the second copy not yet written when the first was.
# ---------------------------------------------------------------------------


def test_the_retry_is_ONE_implementation_shared_by_both_callers():
    """MUTATION: give find_a_holder its own retry loop and this fails.

    Asserted structurally: read_one_transaction must not contain a retry of its own, because the
    version that did is what left find_a_holder without one.
    """
    source = Path(solana_chain_check.__file__).read_text(encoding="utf-8")
    body = source[source.index("def read_one_transaction("):source.index("def hunt_one_program_id(")]
    assert "call_with_backoff(" in body, "the wrapper must delegate"
    assert "for attempt in range" not in body, "a second retry loop is the defect returning"
    assert source.count("for attempt in range") == 1, "exactly one retry loop in this file"


# test_a_throttled_holder_lookup_blames_the_ENDPOINT_and_nothing_else STOOD HERE AND IS GONE
# (rule 9). It seeded only getTokenLargestAccounts as throttled and asserted the message blamed
# the endpoint -- correct when a throttle on that route was terminal. Since 2026-10-01 it falls
# back to the mint's own traffic, so that seed no longer reaches the message it was checking, and
# its one distinct assertion ("never measured" must not appear in a throttle's wording) moved
# into test_BOTH_routes_throttled_reads_differently_from_either_one_alone, which seeds the state
# that actually produces it. Two tests for one rule is rule 8's defect with a delay on it, and
# keeping this one would have meant seeding a fallback it was not about.


def test_a_throttle_is_RETRIED_before_it_is_reported(monkeypatch):
    """It did not retry at all, and a 429 is the one status where asking again shortly works."""
    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    attempts = []

    def throttle_then_answer(*_params):
        attempts.append(1)
        if len(attempts) < 3:
            raise SolanaRPCError("getTokenLargestAccounts returned HTTP 429", status_code=429)
        return {"value": [{"address": _A_HOLDING_ACCOUNT, "uiAmountString": "12.5"}]}

    adapter = _seeded_adapter({
        "getTokenLargestAccounts": throttle_then_answer,
        "getAccountInfo": {"value": {"data": {"parsed": {"info": {"owner": _A_HOLDER}}}}},
    })
    found, _why = find_a_holder(adapter, _A_MINT)
    assert found == _A_HOLDER
    assert len(attempts) == 3, f"asked {len(attempts)} times; the retry is what makes this work"


def test_a_throttle_MID_LOOP_stops_rather_than_walking_past_it(monkeypatch):
    """Walking past a throttled owner lookup would read as "this holder has no owner field".

    That is the finding-versus-endpoint confusion one level down: the loop would exhaust the
    list against an endpoint that has started refusing, then report a shape finding.
    """
    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    adapter = _seeded_adapter({
        "getTokenLargestAccounts": {"value": [{"address": _A_HOLDING_ACCOUNT},
                                              {"address": "SECOND"}]},
        "getAccountInfo": _throttle_always,
    })
    found, why = find_a_holder(adapter, _A_MINT)
    assert found == ""
    assert "THROTTLED the owner lookup" in why
    assert _A_HOLDING_ACCOUNT in why, "name the entry it was on"
    assert "holders were read; who owns them was not" in why
    assert "never measured" not in why


def test_owner_of_separates_a_throttle_from_an_unusable_entry():
    """The extracted decision, directly: three outcomes and the caller treats them differently."""
    good = _seeded_adapter({"getAccountInfo": {"value": {"data": {"parsed": {"info": {
        "owner": _A_HOLDER}}}}}})
    assert owner_of(good, _A_HOLDING_ACCOUNT) == (_A_HOLDER, False, "")

    empty = _seeded_adapter({"getAccountInfo": {"value": {"data": {"parsed": {"info": {}}}}}})
    owner, throttled, why = owner_of(empty, _A_HOLDING_ACCOUNT)
    assert (owner, throttled) == ("", False)
    assert "data.parsed.info" in why, "name the path, so a shape change is diagnosable"

    bad = _seeded_adapter({"getAccountInfo": {"value": {"data": {"parsed": {"info": {
        "owner": "not-an-address"}}}}}})
    owner, throttled, why = owner_of(bad, _A_HOLDING_ACCOUNT)
    assert (owner, throttled) == ("", False)
    assert "not a valid Solana address" in why


def test_call_with_backoff_does_not_retry_a_failure_that_waiting_cannot_fix():
    """A 503 or a bad shape is not a rate limit, and retrying it just multiplies the wait."""
    attempts = []

    def unwell(*_params):
        attempts.append(1)
        raise SolanaRPCError("returned HTTP 503 from devnet: node is unwell", status_code=503)

    adapter = _seeded_adapter({"getSlot": unwell})
    result, throttled, why = call_with_backoff(adapter, "getSlot")
    assert result is None
    assert throttled is False, "503 is not a rate limit"
    assert "503" in why
    assert len(attempts) == 1


def _throttle_always(*_params):
    raise SolanaRPCError("returned HTTP 429 from devnet", status_code=429)

# ---------------------------------------------------------------------------
# THE FALLBACK ROUTE. getTokenLargestAccounts is throttled on public devnet --
# measured, from the operator's runs on 2026-09-30 and 2026-10-01: HTTP 429
# after three attempts, twice in a row. A flag that cannot get past a rate
# limit is a flag that does not work.
#
# The fallback uses only methods this cluster HAS answered (getSignaturesFor-
# Address and getTransaction, both proven by --hunt-memo) and reads
# meta.postTokenBalances[].owner/.mint -- the exact keys _spl_credits selects
# on. So an answer from it measures the unproven path as a side effect.
# ---------------------------------------------------------------------------


def _a_mint_transaction(owner=_A_HOLDER, mint=_A_MINT):
    return {"meta": {"postTokenBalances": [{"mint": mint, "owner": owner}]}}


def test_the_selection_reads_THE_SAME_TWO_KEYS_spl_credits_uses():
    """Pure, seeded, no cluster. If this path is right, _spl_credits' selection is right.

    MUTATION: read `.address` instead of `.owner`, or drop the mint comparison, and this fails.
    Spelling the keys differently from chains/solana._spl_credits would make the whole
    measurement meaningless -- which is why both sites say so.
    """
    assert owner_in_post_token_balances(_a_mint_transaction(), _A_MINT) == _A_HOLDER
    assert owner_in_post_token_balances(_a_mint_transaction(mint="OTHER"), _A_MINT) == "", (
        "an entry for a DIFFERENT mint is not a holder of this one"
    )
    assert owner_in_post_token_balances(_a_mint_transaction(owner="not-an-address"), _A_MINT) == ""
    assert owner_in_post_token_balances({"meta": {"postTokenBalances": []}}, _A_MINT) == ""
    assert owner_in_post_token_balances({"meta": {}}, _A_MINT) == ""
    assert owner_in_post_token_balances("not a transaction", _A_MINT) == ""


def test_the_fallback_answers_when_the_precise_route_is_throttled(monkeypatch):
    """THE OPERATOR'S SITUATION, REPRODUCED: 429 on the first route, an answer from the second."""
    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    adapter = _seeded_adapter({
        "getTokenLargestAccounts": _throttle_always,
        "getSignaturesForAddress": [{"signature": "5abcDEF1234567890xyz"}],
        "getTransaction": _a_mint_transaction(),
    })
    found, why = find_a_holder(adapter, _A_MINT)
    assert found == _A_HOLDER
    assert "was throttled" in why, "say the first route failed"
    assert "mint's own traffic instead" in why, "and which route actually produced the address"
    assert "MEASURES `meta.postTokenBalances[].owner`" in why, (
        "getting an answer this way measures the previously unproven key path -- say so"
    )


def test_BOTH_routes_throttled_reads_differently_from_either_one_alone(monkeypatch):
    """Three outcomes, three next actions, and only one of them is about our code."""
    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    both = _seeded_adapter({"getTokenLargestAccounts": _throttle_always,
                            "getSignaturesForAddress": _throttle_always})
    _found, why = find_a_holder(both, _A_MINT)
    assert "BOTH routes were throttled" in why
    assert "never answered either way" in why
    assert "NOTHING about the field names" in why
    assert "Re-run in a moment" in why
    # THE ASSERTION INHERITED FROM THE TEST THIS REPLACED: a throttle must never carry the
    # field-name caveat. That sentence is what the operator read on 2026-09-30 for what was
    # simply a rate limit, and it sends a reader to audit code that is probably fine.
    assert "never measured" not in why

    quiet = _seeded_adapter({"getTokenLargestAccounts": _throttle_always,
                             "getSignaturesForAddress": []})
    _found, quiet_why = find_a_holder(quiet, _A_MINT)
    assert "DID answer and found nothing usable" in quiet_why
    assert "no recent signatures of its own" in quiet_why
    assert "BOTH routes" not in quiet_why
    assert quiet_why != why


def test_a_quiet_mint_is_a_fact_about_the_MINT_and_not_about_a_field_name():
    """Transfers reference token accounts; only some operations put the mint in the keys.

    MUTATION: report an empty signature list as a shape finding and this fails -- it would send
    a reader to audit postTokenBalances over a mint that simply has no direct traffic.
    """
    adapter = _seeded_adapter({"getSignaturesForAddress": []})
    found, throttled, why = holder_from_mint_traffic(adapter, _A_MINT)
    assert (found, throttled) == ("", False)
    assert "not about any field name" in why
    assert "Transfers reference token accounts" in why


def test_reading_transactions_and_finding_no_entry_IS_a_shape_finding():
    """The other empty case, and it IS about our keys -- transactions existed and carried none."""
    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": "sigA"}, {"signature": "sigB"}],
        "getTransaction": {"meta": {"postTokenBalances": []}},
    })
    found, throttled, why = holder_from_mint_traffic(adapter, _A_MINT)
    assert (found, throttled) == ("", False)
    assert "read 2 of the mint's transactions" in why, "rule 3: the denominator"
    assert "those are the keys _spl_credits uses" in why


def test_one_unreadable_transaction_does_not_end_the_search(monkeypatch):
    """MUTATION: return on the first failure and a single odd row hides every good one behind it."""
    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    seen = []

    def sometimes(signature, _config):
        seen.append(signature)
        if signature == "BAD":
            raise SolanaRPCError("getTransaction returned HTTP 500", status_code=500)
        return _a_mint_transaction()

    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": "BAD"}, {"signature": "GOOD"}],
        "getTransaction": sometimes,
    })
    found, throttled, _why = holder_from_mint_traffic(adapter, _A_MINT)
    assert (found, throttled) == (_A_HOLDER, False)
    assert seen == ["BAD", "GOOD"]


def test_a_throttle_mid_fallback_stops_rather_than_reading_the_rest(monkeypatch):
    """Same rule as the other route: retrying the remainder against a refusing endpoint is waste,
    and walking past would make the throttle look like a missing field."""
    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": "sigA"}, {"signature": "sigB"}],
        "getTransaction": _throttle_always,
    })
    found, throttled, why = holder_from_mint_traffic(adapter, _A_MINT)
    assert (found, throttled) == ("", True)
    assert "was throttled" in why
    assert "keys _spl_credits uses" not in why, "a throttle is not a shape finding"

def test_an_unfetched_signature_is_NOT_counted_as_filtered_over():
    """THE OPERATOR'S 2026-10-01 RUN, AND THE FOURTH LOG-ONLY FACT SURFACED IN TWO DAYS.

    One getTransaction answered HTTP 429. The adapter skipped it correctly and said so in its
    log -- "read 9 of 10 listed transaction(s); 1 were unreadable ... A deposit in any of them is
    NOT credited" -- and this summary still reported the filter as having run over all ten and
    matched nothing. A credit may be in the one that was never fetched, so "matched nothing" was
    not established over the set it named.

    Same shape as the unattributable drops and the rate limits before it: a fact the adapter
    knew, wrote to a log, and did not expose, so the caller reported a conclusion it had not
    earned. `signatures_read` is a PROPERTY now -- listed minus unreadable -- so the name cannot
    claim more than happened, and the unread count is named separately rather than folded in.

    MUTATION: count listed instead of fetched, or drop the `unreadable` clause, and this fails.
    """
    partial = CreditPathObserved(address_read=True, is_spl=True, signatures=9,
                                 credits=0, refused=0, unreadable=1)
    text = " ".join(credit_path_lines(partial))
    assert "9 FETCHED signature(s)" in text
    assert "1 LISTED but never fetched" in text
    assert "NOT established over the full set" in text

    complete = CreditPathObserved(address_read=True, is_spl=True, signatures=10,
                                  credits=0, refused=0, unreadable=0)
    whole = " ".join(credit_path_lines(complete))
    assert "10 FETCHED signature(s)" in whole
    assert "never fetched" not in whole, "nothing was skipped; do not say it was"
    assert text != whole, "a partial scan and a complete one must not read the same"


def test_the_deposits_line_names_what_it_could_not_fetch():
    """And the step's own line too, not just the summary -- that is where a reader looks first."""
    partial = _DepositStub(signatures_read=9, unreadable=["5tG3oZnjMXYr"])
    line = _deposits_line(partial, "rADDR", 10)
    assert "9 signature(s) FETCHED" in line
    assert "1 of 10 listed could NOT be fetched" in line
    assert "not ruled out" in line, "an unfetched transaction leaves the question open"

    complete = _DepositStub(signatures_read=10)
    assert "could NOT be fetched" not in _deposits_line(complete, "rADDR", 10)


def test_signatures_read_is_listed_minus_unfetched_on_the_real_adapter():
    """The property, on the real class, because the stub proves only the stub.

    MUTATION: assign signatures_read = len(signatures) again -- which is what it was -- and this
    fails. That assignment is the whole defect: a name saying `read` while counting `listed`.
    """
    adapter = chains_solana.SolanaAdapter(url="http://seeded.invalid")
    adapter.signatures_listed = 10
    adapter.unreadable_signatures = ["a", "b"]
    assert adapter.signatures_read == 8
    adapter.unreadable_signatures = []
    assert adapter.signatures_read == 10

def test_check_address_carries_the_unread_count_into_the_summary(monkeypatch, capsys):
    """END TO END: a skipped transaction reaches the coverage report, not just the adapter.

    MUTATION: hardcode `unreadable=0` in the observation and this fails. That mutation SURVIVED
    the first run of this change, because nothing drove a skipped transaction through
    check_address -- the summary could have gone on overstating its coverage with every unit
    test green, which is exactly how the original defect shipped.
    """
    address = SOLANA_DEVNET_ACCOUNT
    calls = {"n": 0}

    class OneThrottled(_WholeClusterStub):
        def _result(self, method):
            if method == "getSignaturesForAddress":
                return [{"signature": "THROTTLED", "confirmationStatus": "confirmed"},
                        {"signature": "FINE", "confirmationStatus": "confirmed"}]
            return super()._result(method)

        def __call__(self, url, data=None, **kwargs):
            payload = _json.loads(data) if data else {}
            if payload.get("method") == "getTransaction":
                calls["n"] += 1
                if payload["params"][0] == "THROTTLED":
                    return _Throttled()
            return super().__call__(url, data=data, **kwargs)

    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    _point_config_at_the_stub(monkeypatch)
    monkeypatch.setattr(chains_solana.requests, "post", OneThrottled())
    monkeypatch.setattr("sys.argv", ["solana_chain_check.py"])
    solana_chain_check.main()
    out = text_of(capsys.readouterr().out)

    assert address in out
    assert "1 FETCHED signature(s)" in out, "one of the two was never fetched"
    assert "1 LISTED but never fetched" in out
    assert "NOT established over the full set" in out
    assert "could NOT be fetched" in out, "the step's own line says it too"


class _Throttled:
    status_code = 429
    text = '{"jsonrpc":"2.0","error":{"code": 429, "message":"Too many requests"}}'

    def json(self):
        raise AssertionError("a non-200 must never be parsed as a result")


def test_the_found_holder_is_never_described_as_a_WALLET():
    """A PDA holds tokens and has no private key, and the operator's run found one.

    find_a_holder returned 218gaMJkJUkzPrfax8vEYKtpm3aj9dHUvYcnRnvKbLLp calling it "a wallet",
    and the ADDRESS section two lines later printed "OFF-CURVE ... No private key exists for
    it". Two lines of one paste contradicting each other, in the script whose job is saying what
    is known.

    MUTATION: put "wallet" back in either message and this fails. It survived the first mutation
    run because nothing asserted the wording -- a comment is not a test.
    """
    adapter = _seeded_adapter({
        "getTokenLargestAccounts": {"value": [{"address": _A_HOLDING_ACCOUNT,
                                               "uiAmountString": "103.03"}]},
        "getAccountInfo": {"value": {"data": {"parsed": {"info": {"owner": _A_HOLDER}}}}},
    })
    _found, why = find_a_holder(adapter, _A_MINT)
    assert "wallet" not in why.lower(), (
        f"a PDA owning a token account is ordinary; calling it a wallet claims a private key "
        f"exists. Message was: {why}"
    )

    fallback = _seeded_adapter({
        "getTokenLargestAccounts": _throttle_always,
        "getSignaturesForAddress": [{"signature": "5abcDEF1234567890xyz"}],
        "getTransaction": _a_mint_transaction(),
    })
    _found, fallback_why = find_a_holder(fallback, _A_MINT)
    assert "wallet" not in fallback_why.lower(), fallback_why