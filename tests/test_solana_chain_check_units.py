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
from chains.solana_memo import MEASURED_MEMO_PROGRAM_IDS, MEMO_PROGRAM_IDS  # noqa: E402

import solana_chain_check  # noqa: E402
from solana_chain_check import (  # noqa: E402 -- the sys.path line above is what puts the repository root on the path; this script lives there (rule 10), not inside the package.
    GENESIS_HASHES,
    MEMO_HUNT_GIVE_UP_AFTER_THROTTLES,
    MEMO_HUNT_RETRIES_PER_READ,
    MEMO_HUNT_TRANSACTION_VERSION,
    _network_line,
    check_rent,
    hunt_one_program_id,
    make_runner,
    memo_status_lines,
    print_banner,
    print_summary,
    read_one_transaction,
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
    monkeypatch.setattr(solana_chain_check, "MEMO_HUNT_BACKOFF_SECONDS", 0)


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
    assert adapter.asked == MEMO_HUNT_RETRIES_PER_READ, (
        f"tried {adapter.asked} times; the per-read cap is {MEMO_HUNT_RETRIES_PER_READ}"
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
    r = MEMO_HUNT_RETRIES_PER_READ
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
    print_summary([], 1.0, hunted=True)
    out = capsys.readouterr().out
    assert "CREDIT path is still unproven" in out
    assert "find_deposits_to_address" in out
    assert "--address" in out and "--mint" in out

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
        return {
            "getHealth": "ok",
            "getVersion": {"solana-core": "4.3.0"},
            "getGenesisHash": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
            "getSlot": 506046940,
            "getEpochInfo": {"epoch": 1171, "slotIndex": 174942, "absoluteSlot": 506046942},
            "getMinimumBalanceForRentExemption": 650240,
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
    expected = {"encoding": "jsonParsed",
                "maxSupportedTransactionVersion": MEMO_HUNT_TRANSACTION_VERSION}
    for body in reads:
        assert body["params"][1] == expected, (
            "the hunt's getTransaction config must be exactly this -- jsonParsed because "
            "memo_strings_in() reads the parsed shape only, and the declared version"
        )