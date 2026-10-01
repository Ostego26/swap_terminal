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
    RevealingTx,
    _deposits_line,
    _indented,
    _network_line,
    _one_reason_per_group,
    _revealed_by,
    _spl_reader_line,
    _what_the_target_is,
    call_with_backoff,
    check_rent,
    coverage_clause,
    credit_path_lines,
    credits_the_owner,
    find_a_holder,
    holder_found_sentence,
    holder_from_mint_traffic,
    hunt_one_program_id,
    make_runner,
    memo_status_lines,
    owner_in_post_token_balances,
    owner_of,
    print_banner,
    print_summary,
    prove_the_spl_reader,
    read_one_transaction,
    resolve_address,
    scan_the_mints_transactions,
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
            # ADDED 2026-10-01, because every SPL main() test through this stub was printing
            # `FAIL balance unexpected KeyError: 'amount'` and passing anyway -- its assertions
            # were on other lines. A stub that bakes in a failure the real cluster does not have
            # teaches the next reader that the SPL balance step is expected to break.
            "getTokenAccountBalance": {"value": {"amount": "103032164467", "decimals": 9,
                                                 "uiAmount": 103.032164467,
                                                 "uiAmountString": "103.032164467"}},
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
#: The operator's 2026-10-01 --find-holder run: the scan read 5 signatures, DECODED one amount
#: and refused it for carrying no memo. That is already a proof of the decoder -- the decode
#: happens before the memo check -- which is why the targeted step is skipped against this.
SPL_DECODED = CreditPathObserved(address_read=True, is_spl=True, signatures=5,
                                 credits=0, refused=1)


def test_with_no_address_the_summary_says_the_credit_path_did_not_run():
    text = summary_text(NOTHING_READ)
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
    text = summary_text(SPL_FILTER_ONLY)
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
    text = summary_text(NATIVE_DECODED)
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
    text = summary_text(CreditPathObserved(address_read=True, is_spl=True, signatures=0, credits=0, refused=0))
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
    assert len({summary_text(o) for o in states}) == 5


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


def summary_text(observed) -> str:
    """credit_path_lines() as one normalized string, for asserting on wording not layout.

    ELEVEN CALL SITES USED `summary_text((...))`, which puts a DOUBLE space at
    every wrap point because each line already carries the block's two-space indent -- so any
    assertion straddling a wrap broke the moment the wrapping changed, and five did when
    _wrapped() replaced the hand-wrapped literals. text_of() was written for exactly this and
    these sites were not using it. One helper, so the next wrap change touches no test.
    """
    # NOT `text_of(summary_text(...))`. The sed that rewrote the eleven call sites matched this
    # line too and made the helper call itself -- ten RecursionErrors, in the commit that was
    # removing hand-maintained formatting. Mechanical rewrites hit their own definition.
    return text_of(" ".join(credit_path_lines(observed)))


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


def _a_drop(credits=1, amount=None):
    """The signature is the real one from the operator's devnet account (2K2Pw1Hz...).

    `amount` defaults to the count rather than to zero: these helpers exist to exercise the
    COUNT, and a 0.0 amount in a fixture reads as "a zero-value deposit is stranded", which is
    the one conclusion UnattributableCredit refuses to let a caller reach by omission.
    """
    return chains_solana.UnattributableCredit(
        signature="2K2Pw1Hz", credits=credits,
        why="no memo instruction -- unattributable, and a human has to match it",
        amount=float(credits) if amount is None else amount,
        address="J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM")


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


def _seeded_adapter(responses, mint=""):
    """A real SolanaAdapter with only its transport replaced, so its logger really fires.

    `mint` MATTERS FOR THE SPL TESTS and defaulting it to "" cost three failures: without one
    `is_spl` is False, so _credits_in_transaction takes the NATIVE path and reads accountKeys
    instead of token balances. A stub adapter in the wrong mode tests the wrong reader.
    """
    adapter = chains_solana.SolanaAdapter(url="http://seeded.invalid", mint=mint)

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

    assert "ZERO signatures were LISTED" in quiet
    assert "no credit to this address in the signatures read" in busy, (
        "and NOT 'none of them credited', whose `them` pointed forward to a count that moved "
        "into coverage_clause() -- a pronoun with no antecedent in front of it"
    )
    assert "All 10 listed signature(s) were FETCHED" in busy, (
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
    native = summary_text(NATIVE_DECODED)
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
    """An ENTRY for this owner and mint, with no amounts -- so it does NOT credit.

    Deliberately minimal, because owner_in_post_token_balances() reads only `mint` and `owner`
    and this seed is what proves it needs nothing else. credits_the_owner() reads the amounts,
    so over this seed it is False, which is the entry-only case the search now walks past.
    """
    return {"meta": {"postTokenBalances": [{"mint": mint, "owner": owner}]}}


def _a_crediting_mint_transaction(owner=_A_HOLDER, mint=_A_MINT, *, before="0", after="7000000000"):
    """The same entry, with a POSITIVE delta -- the transaction the targeted proof wants.

    The distinction between this and _a_mint_transaction() is the one the operator's 2026-10-01
    run exposed: the search settled for the entry-only shape and the proof step was aimed at a
    transaction that could not decode an amount.
    """
    def side(amount):
        return [{"accountIndex": 1, "mint": mint, "owner": owner,
                 "uiTokenAmount": {"amount": amount, "decimals": 9}}]

    return {"meta": {"preTokenBalances": side(before), "postTokenBalances": side(after)}}


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
    text = summary_text(partial)
    assert "9 FETCHED signature(s)" in text
    assert "1 LISTED but never fetched" in text
    assert "NOT established over the full set" in text

    complete = CreditPathObserved(address_read=True, is_spl=True, signatures=10,
                                  credits=0, refused=0, unreadable=0)
    whole = summary_text(complete)
    assert "10 FETCHED signature(s)" in whole
    assert "never fetched" not in whole, "nothing was skipped; do not say it was"
    assert text != whole, "a partial scan and a complete one must not read the same"


def test_the_deposits_line_names_what_it_could_not_fetch():
    """And the step's own line too, not just the summary -- that is where a reader looks first."""
    partial = _DepositStub(signatures_read=9, unreadable=["5tG3oZnjMXYr"])
    line = _deposits_line(partial, "rADDR", 10)
    assert "9 of 10 listed signature(s) were fetched" in line
    assert "1 could NOT be fetched" in line
    assert "NOT ruled out" in line, "an unfetched transaction leaves the question open"

    complete = _DepositStub(signatures_read=10)
    assert "could NOT be fetched" not in _deposits_line(complete, "rADDR", 10)


def test_an_account_whose_every_transaction_was_throttled_is_not_called_untouched():
    """Three signatures LISTED, none fetchable. "Nothing has touched this account" is the
    opposite conclusion, and it is the one that hides a deposit.

    The branch tested `signatures_read` -- listed MINUS unfetched -- so an account on a
    rate-limited endpoint came out at read=0 and was reported as quiet. Found 2026-10-01 while
    giving the stranded-money branch its denominator.

    MUTATION: key on signatures_read again and this fails.
    """
    line = _deposits_line(
        _DepositStub(signatures_read=0, unreadable=["5tG3oZnjMXYr", "4yPFj1mqTVnx", "65bWBunzbN"]),
        "rADDR", 10)
    assert "nothing has touched this account" not in line, (
        "three transactions touched it; none could be read, which is not the same thing"
    )
    assert "0 of 3 listed signature(s) were fetched" in line
    assert "3 could NOT be fetched" in line and "NOT ruled out" in line


def test_the_stranded_money_branch_carries_its_denominator_too():
    """The branch that was missing it, and the one where it matters most.

    Your 2026-10-01 run printed `find_deposits_to_address(limit=5)` on the step line and
    `over 4 signature(s)` in the SUMMARY, with nothing reconciling them -- 4 listed, or 5 listed
    with one unfetched? The adapter knew both numbers and this branch did not ask.

    MUTATION: drop the coverage_clause() call here and the stranded report goes back to having
    no denominator, which is rule 3's named failure.
    """
    stranded = _deposits_line(
        _DepositStub(drops=[_a_drop(credits=1)], signatures_read=4,
                     unreadable=["5tG3oZnjMXYr"]), "rADDR", 5)
    assert "WERE READ AND REFUSED" in stranded, "still reported first and as a problem"
    assert "\n      4 of 5 listed" in stranded, (
        "on its own indented line, like every other element of this block -- appended to the "
        "sentence above it made a ~300-character wall the terminal wrapped mid-clause"
    )
    assert "4 of 5 listed signature(s) were fetched" in stranded
    assert "1 could NOT be fetched" in stranded and "NOT ruled out" in stranded, (
        "a stranded credit is already proven here, so a deposit in an unfetched transaction is "
        "a live possibility rather than a caveat"
    )

    full = _deposits_line(_DepositStub(drops=[_a_drop(credits=1)], signatures_read=5), "rADDR", 5)
    assert "All 5 listed signature(s) were FETCHED" in full
    assert "could NOT be fetched" not in full


def test_the_coverage_clause_names_the_limit_only_when_it_differs_from_what_was_listed():
    """Three numbers that are not the same number, and the operator reads the screen.

    `limit` is what was ASKED for; `signatures_listed` is what the endpoint returned; fewer than
    the limit is the endpoint having no more to give, not an error -- so the limit is named only
    where the gap would otherwise be unexplained. Which is exactly what your run needed: the
    step line said limit=5 and the summary said 4.
    """
    matched = coverage_clause(_DepositStub(signatures_read=5), 5)
    assert "All 5 listed signature(s) were FETCHED, so the window" in matched
    assert "limit asked for" not in matched, "nothing to explain when the two agree"

    short = coverage_clause(_DepositStub(signatures_read=4), 5)
    assert "All 4 listed signature(s) were FETCHED (the limit asked for 5)" in short, (
        "this is your run's case, and the sentence it was missing"
    )


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

# ---------------------------------------------------------------------------
# FORTY-TWO IDENTICAL WARNINGS. The operator's --limit 50 run on 2026-10-01:
# 42 of 50 transactions throttled, and the step printed all 42 in full --
# six lines each, ~300 characters each, every one saying the same thing about
# a different signature. The line that mattered ("read 8 of 50 listed") was
# at the bottom of them.
#
# Rule 14 says silence is a defect; this is the same defect from the other
# side, and the memo hunt already fixed it once with "one line for the first,
# a count for the rest". Third time one of these lessons has had to be applied
# in a second place.
# ---------------------------------------------------------------------------

_SIGS = ["5pJoHCc2dkWgEQuHp2Fb5jt3DGkBH9w4hnKQnWwnKZgh",
         "2aKDXDAnigNcyztHaQrSGPKTigiBygttNt76nYVXCa8L",
         "57xwuZsVVN3sETxe9PDcihKs8w7yk9Dgdy6yvTUGQfTa"]
_ACCT = "GcBBd25Sgu2w4EbL9otjxYZ56toWfNaQrKCtFG55CCZx"


def _throttle_warnings(signatures=_SIGS):
    return [f"SOL deposit scan could not read transaction {sig} for account {_ACCT} and SKIPPED "
            f"it: getTransaction returned HTTP 429. Nothing was credited from it."
            for sig in signatures]


def test_records_that_differ_only_by_signature_are_ONE_group():
    """The grouping key is the record with its base58 tokens blanked.

    Two warnings differing only in which transaction was throttled are one piece of information
    repeated. Two differing in their REASON are two findings and both have to be read.
    """
    groups = _one_reason_per_group(_throttle_warnings())
    assert len(groups) == 1
    first, named = groups[0]
    assert _SIGS[0] in first
    assert set(_SIGS) <= set(named), "every signature the group named is kept"


def test_records_with_DIFFERENT_reasons_stay_separate():
    """MUTATION: group by count or by the first N characters and two findings become one."""
    mixed = [
        *_throttle_warnings([_SIGS[0]]),
        f"SOL deposit scan could not read transaction {_SIGS[1]} for account {_ACCT} and SKIPPED "
        f"it: getTransaction returned -32015 Transaction version (1) is not supported.",
    ]
    assert len(_one_reason_per_group(mixed)) == 2, (
        "a rate limit and an unsupported transaction version are different findings"
    )

    # SAME LENGTH, DIFFERENT REASON -- and this pair is why. `key = str(len(record))` survived
    # the mutation run against the pair above, because those two records happen to differ in
    # length, so a key that ignores the text entirely still separated them. A test that passes
    # for a reason it is not about is the thing that lets a wrong key ship.
    throttled = f"transaction {_SIGS[0]} for account {_ACCT}: throttled, nothing credited"
    unsupported = f"transaction {_SIGS[1]} for account {_ACCT}: version unsupported, none credited"
    # PADDED BY COMPUTATION, NOT BY EYE. Written out by hand these came to 158 and 155 and the
    # equal-length assertion failed on its own seed -- which is the same "plausible but wrong
    # test data" that the base58 suffixes hit two tests up.
    width = max(len(throttled), len(unsupported))
    same_length = [throttled.ljust(width, "x"), unsupported.ljust(width, "x")]
    assert len(same_length[0]) == len(same_length[1]), "the point of this pair is equal length"
    assert len(_one_reason_per_group(same_length)) == 2, (
        "grouped by length rather than by what the records SAY"
    )

    # And the converse: differing ONLY in signature must still be one group even when that
    # changes nothing about length, which is the normal case.
    assert len(_one_reason_per_group(_throttle_warnings(_SIGS[:2]))) == 1


def test_forty_two_identical_warnings_print_as_ONE_plus_the_signatures():
    """THE DEFECT, PINNED AT SCALE.

    MUTATION: print every record in full -- which is what it did -- and this fails on the line
    count. Forty-two six-line blocks is output that says nothing a reader can act on, at volume.
    """
    # VALID BASE58 SUFFIXES. The first version of this built them with f"{n:02d}", which
    # produces "00", "01" ... -- and `0` is NOT in the base58 alphabet, so the token split and
    # the grouping key differed per record. The test failed on its own seed rather than on the
    # code, which is the cheap version of the same mistake: invalid data that looks plausible.
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    many = _throttle_warnings([
        f"{sig[:-2]}{alphabet[n // len(alphabet)]}{alphabet[n % len(alphabet)]}"
        for n, sig in enumerate(_SIGS * 14)
    ])
    folded = _indented(many)
    assert folded.count("logged:") == 1, (
        f"printed {folded.count('logged:')} full records for one repeated reason"
    )
    assert "and the SAME reason for 41 more" in folded


def test_EVERY_signature_is_still_printed_because_recovery_needs_them():
    """Shortening the block by dropping evidence would trade one unusable output for another.

    An operator recovering a deposit by hand needs the signature. What is removed is the
    repetition of the REASON, never the signatures.

    MUTATION: truncate the signature list to the first few and this fails.
    """
    folded = _indented(_throttle_warnings())
    for sig in _SIGS:
        assert sig in folded, f"{sig} is how a human finds that transaction again"
    assert "for recovery by hand" in folded


def test_the_account_named_in_every_record_is_not_repeated_as_a_signature():
    """It is in the full record already; listing it again as "one more" would be noise.

    MUTATION: collect every base58 token without excluding the ones already shown, and the
    account address appears in the recovery list once per skipped transaction.
    """
    folded = _indented(_throttle_warnings())
    assert folded.count(_ACCT) == 1, (
        f"the account appears {folded.count(_ACCT)} times; it is the same account every line"
    )


def test_a_single_record_is_unchanged():
    """No collapsing to do, and no "and 0 more" line either."""
    folded = _indented(_throttle_warnings([_SIGS[0]]))
    assert folded.count("logged:") == 1
    assert "SAME reason" not in folded
    assert "for recovery by hand" not in folded


def test_a_BIGGER_window_is_reported_as_covering_LESS():
    """THE SECOND FINDING FROM THAT RUN, AND I CAUSED IT -- I suggested --limit 50.

    MEASURED on the operator's two runs against public devnet:

        --limit 10    9 of 10 fetched
        --limit 50    8 of 50 fetched

    Every listed signature costs a getTransaction, so raising the limit spends the rate budget
    on listing and FEWER transactions come back. That is the opposite of everyone's instinct,
    which is why it has to be on the screen next to the number -- an operator reading "42 never
    fetched" reaches for a bigger window.

    MUTATION: drop the advice, or trigger it whenever anything was unfetched, and this fails --
    the second would nag on a healthy run where the endpoint is coping fine.
    """
    starved = summary_text(CreditPathObserved(address_read=True, is_spl=True, signatures=8,
                           credits=0, refused=0, unreadable=42))
    assert "SMALLER --limit will cover MORE" in starved
    assert "the endpoint is the limit, not the window" in starved

    coping = summary_text(CreditPathObserved(address_read=True, is_spl=True, signatures=9,
                           credits=0, refused=0, unreadable=1))
    assert "SMALLER --limit" not in coping, "one skipped read is not a starved endpoint"
    assert "never fetched" in coping, "but it is still named"


def test_the_coverage_line_does_not_print_a_full_stop_before_a_comma():
    """Punctuation, and it reached the operator-facing line as "full set.,".

    Small, and the reason it is pinned rather than just fixed: the comma belongs to the no-skip
    case only, which is exactly the kind of conditional punctuation that comes back.
    """
    for observed in (CreditPathObserved(True, True, 8, 0, 0, 42),
                     CreditPathObserved(True, True, 10, 0, 0, 0),
                     CreditPathObserved(True, True, 10, 0, 0, 1)):
        text = summary_text(observed)
        assert ".," not in text
        # THE GLUE IS GONE, NOT JUST THE SYMPTOM. `skipped or ','` existed to attach a
        # fragment to a count, and an optional clause between the two then orphaned the
        # fragment -- the 42-unfetched block read "...not the window. and matched nothing",
        # a lowercase continuation after a full stop. Every piece is a whole sentence now,
        # so this asserts the general form rather than the one instance.
        for sentence in text.split(". "):
            assert sentence[:1] == sentence[:1].upper(), (
                f"'{sentence[:40]}' continues after a full stop in lower case, which means a "
                f"fragment is being glued to whatever happens to precede it"
            )

# ---------------------------------------------------------------------------
# THE TARGETED PROOF OF _spl_credits. Four runs failed to prove this reader by
# scanning, and the operator's 2026-10-01 output shows why rather than leaving
# it to guesswork:
#
#   GcBBd25S...  the holder --find-holder found in the MINT's traffic
#   GzprPkmd...  its ASSOCIATED token account -- DOES NOT EXIST
#   0.0          so its WSOL lives in some OTHER token account
#   5 of 5 fetched, filter matched nothing
#
# The scan reads the OWNER's recent signatures; the transaction that revealed
# the owner came from the MINT's history and need not be in that window at all.
# Widening it makes coverage worse on a rate-limited endpoint (8 of 50 against
# 9 of 10). So read the one transaction already known to contain the entry.
# ---------------------------------------------------------------------------

_SPL_MEMO = {"program": "spl-memo", "parsed": "4242",
             "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"}


def _spl_transaction(before, after, *, memo=False):
    """A real-shaped getTransaction whose token balance for _A_HOLDER moves by after-before."""
    entry = {"accountIndex": 1, "owner": _A_HOLDER, "mint": _A_MINT}
    return {
        "meta": {
            "preTokenBalances": [{**entry, "uiTokenAmount": {"amount": before, "decimals": 9}}],
            "postTokenBalances": [{**entry, "uiTokenAmount": {"amount": after, "decimals": 9}}],
            "err": None,
        },
        "transaction": {"message": {"instructions": [_SPL_MEMO] if memo else []}},
    }


def test_a_decoded_and_attributed_credit_says_the_reader_is_proven():
    adapter = _seeded_adapter(mint=_A_MINT, responses={"getTransaction": _spl_transaction("0", "2500000000", memo=True)})
    line = _spl_reader_line(adapter, _A_HOLDER, "4yPFj1mq")
    assert "DECODED and ATTRIBUTED" in line
    assert "2.5" in line, "the decoded amount, from uiTokenAmount.amount and .decimals"
    assert "vout=4242" in line, "the memo tag became the discriminator"


def test_a_DECODED_THEN_REFUSED_credit_still_PROVES_the_decoder():
    """THE DEFECT THIS FIXED, AND IT IS THE FIFTH REPEAT OF ONE OF THIS SESSION'S LESSONS.

    The first version returned "the entry exists but its delta is not POSITIVE" whenever the
    credit list was empty -- and an empty list ALSO happens when the amount decoded fine and
    _attributable dropped it for carrying no memo. Driven against a response crediting 2.5
    tokens it printed "delta is not POSITIVE" directly under the adapter's own WARNING saying
    "1 credit(s) dropped". The decode had happened and the message denied it.

    `unattributable_drops` was added two days ago for exactly this distinction. Every field name
    in the reader had to be right to produce an amount for the memo check to then decline, so a
    refusal is PROOF of the decoder, not silence.

    MUTATION: ignore the drops and report the no-delta wording, which is what it did.
    """
    adapter = _seeded_adapter(mint=_A_MINT, responses={"getTransaction": _spl_transaction("0", "2500000000")})
    line = _spl_reader_line(adapter, _A_HOLDER, "4yPFj1mq")
    assert "DECODED 1 credit(s) and then REFUSED" in line
    assert "are PROVEN" in line
    assert "not POSITIVE" not in line, (
        "the amount WAS decoded -- that sentence is the defect this test exists for"
    )
    assert "which is correct" in line, "refusing an unattributable credit is the right behavior"


def test_no_positive_delta_is_reported_as_a_fact_about_the_TRANSACTION():
    """The one case where the decoder genuinely did not run, and it must not read as proof."""
    adapter = _seeded_adapter(mint=_A_MINT, responses={"getTransaction": _spl_transaction("5", "5")})
    line = _spl_reader_line(adapter, _A_HOLDER, "4yPFj1mq")
    assert line.startswith("(none)")
    assert "returned before decoding an amount" in line
    assert "not about the field names" in line
    assert "PROVEN" not in line
    assert "NOT the same as the reader never running" in line


def test_the_three_outcomes_are_distinguishable():
    adapter_lines = []
    for before, after, memo in (("0", "2500000000", True), ("0", "2500000000", False), ("5", "5", False)):
        adapter = _seeded_adapter(mint=_A_MINT, responses={"getTransaction": _spl_transaction(before, after, memo=memo)})
        adapter_lines.append(_spl_reader_line(adapter, _A_HOLDER, "4yPFj1mq"))
    assert len(set(adapter_lines)) == 3


def test_the_drops_are_cleared_so_an_earlier_step_cannot_be_misread_as_this_one():
    """MUTATION: drop the clear and a drop recorded by the ADDRESS section is reported here.

    Only find_deposits_to_address() clears that list, and this calls the per-transaction reader
    directly -- so without the clear, a run whose scan dropped a credit would claim this
    transaction proved the decoder when it decoded nothing.
    """
    adapter = _seeded_adapter(mint=_A_MINT, responses={"getTransaction": _spl_transaction("5", "5")})
    adapter.unattributable_drops = [
        chains_solana.UnattributableCredit("earlier", 3, "no memo", 3.0, "rEARLIER")]
    line = _spl_reader_line(adapter, _A_HOLDER, "4yPFj1mq")
    assert line.startswith("(none)"), "a stale drop must not be read as this transaction's"
    assert "PROVEN" not in line


def test_the_revealing_signature_is_carried_as_DATA_not_parsed_from_prose():
    """holder_from_mint_traffic records it, so the proof step does not re-read the sentence.

    MUTATION: drop the _revealed_by assignment and prove_the_spl_reader() silently does nothing
    -- the step vanishes from the output and the reader stays unproven with no line saying so.
    """
    _revealed_by.clear()
    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": "65bWBunzbNMkN9d5"}],
        "getTransaction": _a_crediting_mint_transaction(),
    })
    found, _throttled, _why = holder_from_mint_traffic(adapter, _A_MINT)
    assert found == _A_HOLDER
    assert _revealed_by[_A_HOLDER] == RevealingTx("65bWBunzbNMkN9d5", credits_owner=True), (
        "the signature AND what it was chosen for -- an entry is not a credit"
    )

    steps = []
    prove_the_spl_reader(adapter, _A_HOLDER,
                         lambda label, _why, fn: steps.append((label, fn)), SPL_FILTER_ONLY)
    assert steps, "the proof step must run when a revealing signature is known"
    assert "65bWBunzbNMkN9d5" in steps[0][0], "and name the transaction it is aimed at"


def test_no_proof_step_runs_when_no_signature_revealed_the_owner():
    """An address the operator typed has no known-good transaction, so there is nothing to aim at.

    MUTATION: fall back to any signature and the step would report on a transaction chosen for
    no reason -- a read that proves nothing while looking like proof.
    """
    _revealed_by.clear()
    steps = []
    prove_the_spl_reader(_seeded_adapter({}), "rTYPED", lambda *a: steps.append(a),
                         SPL_FILTER_ONLY)
    assert steps == []


def test_main_prints_no_proof_step_for_an_address_the_operator_supplied(monkeypatch, capsys):
    """--mint WITHOUT --find-holder: nothing revealed this address, so nothing is aimed at it.

    This is the operator's ordinary SPL run, and it must not grow a proof step that reports on
    a transaction picked out of the address's recent window -- that read proves the window had
    a credit in it, not that the decoder works, while printing in the same shape as the real
    proof.

    It does NOT kill a mutation of the old `if args.find_holder and adapter.is_spl:` call site,
    and that is the finding rather than a gap in it: both halves were further copies of the
    guard `_revealed_by.get(owner)` already applies, so deleting either changed no behavior and
    no test could have killed them. They are gone and the call site is unconditional. What this
    pins is the OUTCOME those copies were there to protect, measured through main().
    """
    _revealed_by.clear()

    class PlainCluster(_WholeClusterStub):
        def _result(self, method):
            if method == "getSignaturesForAddress":
                return [{"signature": "65bWBunzbNMkN9d5", "confirmationStatus": "finalized"}]
            if method == "getAccountInfo":
                return {"value": {"data": {"parsed": {"info": {"decimals": 9,
                                                               "owner": _A_HOLDER}}},
                                  "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}
            return super()._result(method)

    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(
        solana_chain_check.Config, "RPC",
        {**solana_chain_check.Config.RPC,
         "SOL": {**solana_chain_check.Config.RPC["SOL"], "url": "http://127.0.0.1:1",
                 "mint": _A_MINT}})
    monkeypatch.setattr(chains_solana.requests, "post", PlainCluster())
    monkeypatch.setattr("sys.argv", ["solana_chain_check.py", "--mint", _A_MINT,
                                     "--address", _A_HOLDER])
    solana_chain_check.main()
    out = text_of(capsys.readouterr().out)

    assert "_spl_credits over" not in out, (
        "no transaction revealed this address, so no targeted proof may claim to run"
    )
    assert "find_deposits_to_address" in out, (
        "and the ordinary SPL steps still run -- the absent step is the targeted proof, not the "
        "whole ADDRESS section"
    )
    assert "FAILED" not in out, f"no step may fail on this seeded cluster: {out}"


def test_main_skips_the_targeted_proof_when_the_scan_already_decoded(monkeypatch, capsys):
    """END TO END. The scan credits 7.0 here, so the targeted read is owed to nobody.

    THIS TEST USED TO ASSERT THE OPPOSITE, and the behavior change is the fix for what the
    operator's 2026-10-01 run printed: find_deposits_to_address decoded an amount, and the
    targeted step then spent a sixth getTransaction to re-establish it -- printing "(none)" two
    lines above a SUMMARY correctly saying the reader was proven. The nearer, more specific line
    is the one a reader believes.

    MUTATION: drop the `observed.decoded_an_amount` branch and the step runs anyway, spending a
    read and contradicting the summary.
    """
    _revealed_by.clear()
    holder = _A_HOLDER

    class HolderCluster(_WholeClusterStub):
        # getTokenLargestAccounts THROTTLES, which is the operator's real situation and the only
        # way the fallback runs. Seeded as {"value": []} first, which is a terminal "no holders"
        # -- a different outcome entirely, and the fallback never fired.
        def __call__(self, url, data=None, **kwargs):
            payload = _json.loads(data) if data else {}
            if payload.get("method") == "getTokenLargestAccounts":
                return _Throttled()
            return super().__call__(url, data=data, **kwargs)

        def _result(self, method):
            if method == "getSignaturesForAddress":
                return [{"signature": "65bWBunzbNMkN9d5", "confirmationStatus": "finalized"}]
            if method == "getTransaction":
                return {
                    "meta": {
                        "preTokenBalances": [{"accountIndex": 1, "owner": holder,
                                              "mint": _A_MINT,
                                              "uiTokenAmount": {"amount": "0", "decimals": 9}}],
                        "postTokenBalances": [{"accountIndex": 1, "owner": holder,
                                               "mint": _A_MINT,
                                               "uiTokenAmount": {"amount": "7000000000",
                                                                 "decimals": 9}}],
                        "err": None,
                    },
                    "transaction": {"message": {"instructions": [_SPL_MEMO]}},
                }
            if method == "getAccountInfo":
                return {"value": {"data": {"parsed": {"info": {"decimals": 9,
                                                               "owner": holder}}},
                                  "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}
            return super()._result(method)

    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(
        solana_chain_check.Config, "RPC",
        {**solana_chain_check.Config.RPC,
         "SOL": {**solana_chain_check.Config.RPC["SOL"], "url": "http://127.0.0.1:1",
                 "mint": _A_MINT}})
    monkeypatch.setattr(chains_solana.requests, "post", HolderCluster())
    monkeypatch.setattr("sys.argv", ["solana_chain_check.py", "--mint", _A_MINT,
                                     "--find-holder"])
    solana_chain_check.main()
    out = text_of(capsys.readouterr().out)

    assert "already PROVEN by find_deposits_to_address" in out, (
        "the skip must say WHY it skipped -- silence here reads as the step having failed"
    )
    assert "65bWBunzbNMkN9d5" in out, "and still name the transaction it would have read"
    assert "_spl_credits over" not in out, "no getTransaction may be spent re-proving it"
    assert "(none)  <- no POSITIVE delta" not in out, (
        "and above all no denial may sit above a summary that says the reader is proven"
    )
    assert "7.0" in out, "the amount, decoded from uiTokenAmount off a seeded-but-real shape"

# ---------------------------------------------------------------------------
# AN ENTRY IS NOT A CREDIT. The selection the holder search was missing.
#
# The operator's 2026-10-01 run, the line that prompted all of this:
#
#   _spl_credits over qGqMZv7Ljqpp7VaZ... ...  <- the ONE transaction known to carry a
#                                                 balance for this owner
#     ok   (none)  <- no POSITIVE delta for this owner and mint in this transaction
#
# and four lines below it, correctly:
#
#   CREDIT path: _spl_credits DECODED a real amount from a real response (0 credited,
#                1 refused over 5 signature(s)).
#
# Both true. The caption promised a balance the selection never checked for, the step was
# aimed at a transaction that could not decode an amount, and the denial sat above the
# summary that said the reader was proven.
# ---------------------------------------------------------------------------


def test_credits_the_owner_requires_a_POSITIVE_delta_not_merely_an_entry():
    """The condition _spl_credits requires, spelled the way _spl_credits spells it.

    MUTATION: `>= 0` instead of `> 0`, or dropping the pre-balance lookup, and a transaction
    where the owner SENT tokens counts as a credit -- which is the selection error this whole
    change exists to fix, re-introduced one level down.
    """
    assert credits_the_owner(_a_crediting_mint_transaction(), _A_MINT, _A_HOLDER)
    assert not credits_the_owner(_a_mint_transaction(), _A_MINT, _A_HOLDER), (
        "an entry with no amounts is not a credit -- this is the operator's qGqMZv7 case"
    )
    assert not credits_the_owner(
        _a_crediting_mint_transaction(before="7000000000", after="0"), _A_MINT, _A_HOLDER,
    ), "the owner SENT tokens: post - pre is negative, and _spl_credits takes neither"
    assert not credits_the_owner(
        _a_crediting_mint_transaction(before="5", after="5"), _A_MINT, _A_HOLDER,
    ), "pre == post is a delta of zero, and `> 0` is what the reader uses"
    assert not credits_the_owner(_a_crediting_mint_transaction(owner="rOTHER"),
                                 _A_MINT, _A_HOLDER), "someone else's credit is not this owner's"
    assert not credits_the_owner(_a_crediting_mint_transaction(mint="OTHERMINT"),
                                 _A_MINT, _A_HOLDER), "a different mint at this mint's decimals"
    assert not credits_the_owner("not a transaction", _A_MINT, _A_HOLDER)
    assert not credits_the_owner({"meta": {}}, _A_MINT, _A_HOLDER)


def test_a_malformed_amount_is_not_a_credit_rather_than_a_crash():
    """A wrong shape must not take the run down, and must not count as a credit either.

    It only ever PREFERS one signature over another, so "not a credit" is a safe answer and
    the owner is returned either way. Narrow exception types, not `except Exception` (rule 12).
    """
    for broken in ({"amount": "not a number", "decimals": 9},
                   {"decimals": 9},
                   "not a dict"):
        transaction = {"meta": {"postTokenBalances": [
            {"accountIndex": 1, "owner": _A_HOLDER, "mint": _A_MINT, "uiTokenAmount": broken},
        ]}}
        assert not credits_the_owner(transaction, _A_MINT, _A_HOLDER), broken


def test_the_search_walks_PAST_an_entry_only_transaction_to_find_a_crediting_one():
    """Two transactions: the first carries an entry, the second credits. It must pick the second.

    MUTATION: return on the first owner found -- which is what it did before 2026-10-01 -- and
    the proof step is aimed at a transaction with no positive delta in it.
    """
    _revealed_by.clear()
    crediting = "65bWBunzbNMkN9d5"
    entry_only = "4yPFj1mqTVnxbKHd"

    # KEYED ON THE SIGNATURE, which _seeded_adapter already supports: a callable response is
    # handed the real params. The order matters -- entry-only FIRST, so returning on the first
    # owner found picks the wrong one.
    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": entry_only}, {"signature": crediting}],
        "getTransaction": lambda signature, *_rest: (
            _a_crediting_mint_transaction() if signature == crediting else _a_mint_transaction()
        ),
    }, _A_MINT)
    found, throttled, why = holder_from_mint_traffic(adapter, _A_MINT)
    assert (found, throttled) == (_A_HOLDER, False)
    assert _revealed_by[_A_HOLDER] == RevealingTx(crediting, credits_owner=True), (
        f"the CREDITING signature, not the first entry seen. why={why}"
    )
    assert "CREDITS this mint" in why


def test_an_entry_only_owner_is_still_returned_when_nothing_in_the_window_credits_it():
    """A fallback, not a gate. The balance and ATA steps want the owner either way.

    MUTATION: require a crediting transaction and --find-holder reports "no holder found" for a
    mint whose window happens to hold no deposit -- losing an owner it had in hand.
    """
    _revealed_by.clear()
    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": "4yPFj1mqTVnxbKHd"}],
        "getTransaction": _a_mint_transaction(),
    }, _A_MINT)
    found, throttled, why = holder_from_mint_traffic(adapter, _A_MINT)
    assert (found, throttled) == (_A_HOLDER, False)
    assert _revealed_by[_A_HOLDER].credits_owner is False
    assert "NONE of the 1 transaction(s) read CREDITS it" in why, (
        "and it says so BEFORE the step runs, so '(none)' below cannot read as a failure"
    )


def test_a_throttle_mid_walk_discards_the_fallback_rather_than_reporting_it():
    """Most of the window was never read, so "nothing credits it" would be an overclaim.

    ASSERTED ON THE SCAN ITSELF, not only through holder_from_mint_traffic(). Returning the
    entry-only candidate alongside the throttle survives a mutation run when only the caller is
    driven, because the caller checks `throttled_why` first and discards whatever came with it.
    That makes the contract unobservable from outside -- so it is pinned where it lives, at the
    seam, rather than left as a comment claiming a behavior nothing can see (rule 17: say which
    you have).
    """
    _revealed_by.clear()
    entry_only, never_read = "4yPFj1mqTVnxbKHd", "65bWBunzbNMkN9d5"

    def throttle_the_second(signature, *_rest):
        if signature == never_read:
            raise chains_solana.SolanaRPCError("rate limited", status_code=429)
        return _a_mint_transaction()

    adapter = _seeded_adapter({
        "getSignaturesForAddress": [{"signature": entry_only}, {"signature": never_read}],
        "getTransaction": throttle_the_second,
    }, _A_MINT)
    found, throttled, why = holder_from_mint_traffic(adapter, _A_MINT)
    assert (found, throttled) == ("", True)
    assert never_read[:16] in why and "throttled" in why
    assert _revealed_by == {}, "a cut-short walk leaves no proof target behind it"

    crediting, entry_only_candidate, throttled_why = scan_the_mints_transactions(
        adapter, _A_MINT, [{"signature": entry_only}, {"signature": never_read}])
    assert throttled_why, "the throttle is reported"
    assert (crediting, entry_only_candidate) == (None, None), (
        "and NEITHER candidate comes back with it. The walk read 1 of 2 transactions, so "
        "'nothing in this window credits it' is a claim about a window it did not read."
    )


def test_the_caption_says_what_the_transaction_was_chosen_FOR():
    """Pure. A caption promising a balance over a transaction chosen for an entry is the defect.

    MUTATION: one caption for both and the operator reads "(none)" under "known to CREDIT this
    owner" and concludes the reader is broken.
    """
    crediting = _what_the_target_is(RevealingTx("65bWBunzbNMkN9d5", credits_owner=True))
    entry_only = _what_the_target_is(RevealingTx("65bWBunzbNMkN9d5", credits_owner=False))
    assert "CREDIT" in crediting and "EXPECTED" not in crediting
    assert "does NOT credit" in entry_only and "EXPECTED" in entry_only, (
        "an empty result over this transaction is the expected outcome, and must be pre-announced"
    )
    assert crediting != entry_only


def test_holder_found_sentence_distinguishes_a_credit_from_an_entry():
    """Pure -- no cluster, no map. The two sentences that must not be one."""
    credit = holder_found_sentence(RevealingTx("65bWBunzbNMkN9d5", credits_owner=True), read=8)
    entry = holder_found_sentence(RevealingTx("65bWBunzbNMkN9d5", credits_owner=False), read=8)
    assert "CREDITS this mint" in credit
    assert "post - pre > 0" in credit, "the condition, named, not just the word 'credit'"
    assert "NONE of the 8 transaction(s) read CREDITS it" in entry, "with its denominator"
    assert "postTokenBalances" in credit and "postTokenBalances" in entry, (
        "both still report the keys the search measured -- that is why --find-holder exists"
    )


def test_the_proof_step_is_skipped_when_the_scan_already_decoded_an_amount():
    """Rule 3: prefer removing work to doing it faster. The scan's refusal IS the proof.

    MUTATION: run it anyway and a getTransaction is spent to re-establish what is established,
    which is how "(none)" came to sit above a summary saying the reader was proven.
    """
    _revealed_by.clear()
    _revealed_by[_A_HOLDER] = RevealingTx("65bWBunzbNMkN9d5", credits_owner=True)
    steps = []
    prove_the_spl_reader(_seeded_adapter({}, _A_MINT), _A_HOLDER,
                         lambda *a: steps.append(a), SPL_DECODED)
    assert steps == [], "no step, so no read"


def test_the_skip_is_announced_rather_than_silent(capsys):
    """Rule 14: a step that did nothing must not look the same as one that was never there.

    MUTATION: `return` without the print and the operator sees the proof step simply absent,
    with no way to tell it was skipped as owed-to-nobody from never having been wired in.
    """
    _revealed_by.clear()
    _revealed_by[_A_HOLDER] = RevealingTx("65bWBunzbNMkN9d5", credits_owner=True)
    prove_the_spl_reader(_seeded_adapter({}, _A_MINT), _A_HOLDER,
                         lambda *a: None, SPL_DECODED)
    out = capsys.readouterr().out
    assert "already PROVEN by find_deposits_to_address" in out
    assert "65bWBunzbNMkN9d5" in out, "name what it would have read, so the skip is checkable"
    assert "1 refused" in out, "and the evidence it is relying on instead"


def test_the_proof_step_still_runs_when_the_scan_decoded_nothing():
    """The case the whole step exists for, and the one four devnet runs kept landing in."""
    _revealed_by.clear()
    _revealed_by[_A_HOLDER] = RevealingTx("65bWBunzbNMkN9d5", credits_owner=True)
    steps = []
    prove_the_spl_reader(_seeded_adapter({}, _A_MINT), _A_HOLDER,
                         lambda label, why, fn: steps.append((label, why)), SPL_FILTER_ONLY)
    assert len(steps) == 1
    assert "_spl_credits over 65bWBunzbNMkN9d5" in steps[0][0]
    assert "CREDIT" in steps[0][1]


# ---------------------------------------------------------------------------
# A SIGNATURE PRINTED TWICE IS NOT A SECOND KIND OF EVIDENCE.
#
# The 2026-10-01 run, after the coverage clause landed. Two stranded credits:
#
#   (none) CREDITED -- but 2 credit(s) across 2 transaction(s) WERE READ AND REFUSED. ...
#     43o7vzDVNQLnQYCt9pJaMX5gkM5zNHVVPQ2AKve6jtCRdf8Tnwaw5AWRYAbGEE3AHEsWqwGt7UffUbqdgneL9JLB
#       1 credit(s) dropped: no memo instruction -- unattributable ...
#     51haz3Du7iKBSeeS8FFab63nsWPqUkfjxZgvdiypjwttPUHmzH9fjHMZK1W4iJxaW8uKmn2k5huVLoDHjDw1mk74
#       1 credit(s) dropped: no memo instruction -- unattributable ...
#     logged: SOL deposit 43o7vzDVNQLnQYCt... CANNOT BE ATTRIBUTED ...
#             ... and the SAME reason for 1 more. Every one, for recovery by hand:
#               51haz3Du7iKBSeeS8FFab...
#
# Two facts, four 88-character base58 strings. "Recovery by hand" is a real need and the
# step's own list is what serves it -- which is why _indented() now takes the set of
# signatures the caller has already printed, and checks the justification instead of
# assuming it.
# ---------------------------------------------------------------------------

_SIG_A = "43o7vzDVNQLnQYCt9pJaMX5gkM5zNHVVPQ2AKve6jtCRdf8Tnwaw5AWRYAbGEE3AHEsWqwGt7UffUbqdgneL9JLB"
_SIG_B = "51haz3Du7iKBSeeS8FFab63nsWPqUkfjxZgvdiypjwttPUHmzH9fjHMZK1W4iJxaW8uKmn2k5huVLoDHjDw1mk74"
#: A third, needed to reach the PARTIAL overlap -- see that test for why two cannot.
_SIG_C = "5rWbN6zPPm6nLnhmasWtsQAfb35sjG8iBBb3LAh9PRHNuL8EQ59GuRykPgG66aY4Cd4a9Rd8jgiz33CkATjtXJoJ"


def _a_drop_warning(signature):
    """The adapter's real wording, taken from the operator's run rather than paraphrased."""
    return (f"SOL deposit {signature} to the shared account CANNOT BE ATTRIBUTED and was NOT "
            f"credited: no memo instruction -- unattributable, and a human has to match it. "
            f"1 credit(s) dropped.")


def test_a_signature_the_step_already_listed_is_not_printed_again():
    """MUTATION: ignore already_listed and the second copy comes back -- your run's defect."""
    block = _indented([_a_drop_warning(_SIG_A), _a_drop_warning(_SIG_B)],
                      already_listed=frozenset({_SIG_A, _SIG_B}))
    assert "and the SAME reason for 1 more, every one already listed above" in block
    assert block.count(_SIG_B) == 0, "the elided one appears nowhere in the block"
    assert block.count(_SIG_A) == 1, (
        "the head record is the adapter's RAW log line and keeps its signature -- that is what "
        "proves the live path's exact wording, and it is one copy rather than two"
    )


def test_with_nothing_listed_above_every_signature_is_still_printed():
    """The no-credit branches print no list of their own, so the block is the only record.

    MUTATION: elide unconditionally and a throttled-window run loses the signatures an operator
    needs to go and look at those transactions by hand.
    """
    block = _indented([_a_drop_warning(_SIG_A), _a_drop_warning(_SIG_B)])
    assert _SIG_B in block, "nothing above it, so nothing may be elided"
    assert "Every one, for recovery by hand" in block
    assert "not listed above" not in block, (
        "that phrasing would send the reader looking for a list they never saw"
    )


def test_only_the_signatures_actually_listed_above_are_elided():
    """A partial overlap keeps the rest, and says the list is the remainder.

    THREE RECORDS, NOT TWO, and the first version of this test used two and failed. The head
    record's OWN signature is already excluded from the repeat list, so with two warnings the
    only candidate is the second one and the overlap can only be total or empty. Reaching the
    partial case takes three: two listed above, one not. I wrote the two-record version from a
    reading of the condition instead of tracing it (rule 17).
    """
    block = _indented(
        [_a_drop_warning(_SIG_A), _a_drop_warning(_SIG_B), _a_drop_warning(_SIG_C)],
        already_listed=frozenset({_SIG_A, _SIG_B}))
    assert _SIG_C in block, "C was never printed above, so it must survive"
    assert _SIG_B not in block, "B was, so it goes"
    assert "Every one not listed above, for recovery by hand" in block


def test_the_stranded_step_elides_what_its_own_list_already_gave():
    """END TO END through _deposits_line, because the wiring is the half that was missing.

    THE STUB HAS TO ACTUALLY LOG. The first version of this used _DepositStub, which logs
    nothing -- so `captured.records` was empty, the block never rendered, and a mutation
    removing the `already_listed=` argument SURVIVED. A test driving the wiring of a log
    capture through something that emits no logs proves the plumbing, not the behavior.

    MUTATION: drop the `already_listed=` argument at the call site and every signature is
    printed twice -- the function right and uncalled, which is the failure mode three
    main()-level mutations in this session have already had.
    """
    class LoggingStub(_DepositStub):
        def find_deposits_to_address(self, address, tx_limit=10):
            # ON THE ADAPTER'S OWN LOGGER, the one _deposits_line attaches its handler to, and
            # at WARNING because that is the level the live drop path uses.
            for drop in self.unattributable_drops:
                logging.getLogger("chains.solana").warning(_a_drop_warning(drop.signature))
            return super().find_deposits_to_address(address, tx_limit)

    def drop(signature):
        return chains_solana.UnattributableCredit(
            signature=signature, credits=1, amount=1.5,
            why="no memo instruction -- unattributable, and a human has to match it",
            address="J5wn3xEMDsr9r8qtF6YTWJodmgW5kG3ZThqDb8Xc37JM")

    line = _deposits_line(
        LoggingStub(drops=[drop(_SIG_A), drop(_SIG_B)], signatures_read=5), "rADDR", 5)
    assert "2 credit(s) across 2 transaction(s) WERE READ AND REFUSED" in line
    assert "logged: SOL deposit" in line, "the capture has to have rendered, or this proves zero"
    assert "every one already listed above" in line
    assert line.count(_SIG_B) == 1, (
        f"{_SIG_B[:16]}... appears {line.count(_SIG_B)} times; it belongs in the step's own "
        f"list and nowhere else"
    )
    assert line.count(_SIG_A) == 2, (
        "once in the step's list, once as the head of the adapter's raw warning -- that head is "
        "what proves the live path's exact wording"
    )


def test_the_summary_never_says_the_OTHER_reader_decoded_nothing():
    """A run reads one reader or the other, so the other one's state is not observable here.

    From the operator's fifth clean --find-holder run, 2026-10-01. The summary ended:

        Every field name in that reader had to be right to get there. The other reader,
        _native_credits (drop --mint), has not decoded one.

    _native_credits was never called in that run. CreditPathObserved holds nothing about it and
    cannot -- `is_spl` decides which reader runs, and only one does. Stated flatly, "has not
    decoded one" reads as a measurement of that reader across every run: rule 17's failure, a
    reason to believe something written in the same voice as having checked it.

    MUTATION: restore "has not decoded one" and this fails on either reader.
    """
    for observed, named, ran in ((SPL_DECODED, "_native_credits (drop --mint)", "_spl_credits"),
                                 (NATIVE_DECODED, "_spl_credits (needs --mint)",
                                  "_native_credits")):
        block = summary_text(observed)
        assert f"{ran} DECODED a real amount" in block, "what this run DID establish"
        assert f"{named}, was NOT exercised by this run" in block
        assert "nothing here says whether it works" in block, (
            "and the limit of the claim, next to the claim (rule 14)"
        )
        assert "has not decoded one" not in block, (
            "a run that never called a reader has measured nothing about it"
        )
        assert "never" in block and "both" in block, (
            "and WHY it was not exercised -- a run reads one or the other, so this is not a gap "
            "the operator left open by accident"
        )


# ---------------------------------------------------------------------------
# THE RUN THAT FIRST EXERCISED THE TARGETED PROOF, 2026-10-01. The scan found nothing, so
# the step ran, and it WORKED -- then the summary said the opposite:
#
#   _spl_credits over 5MVQ2U12Y8NcdV7y... ...
#     ok   DECODED 1 credit(s) and then REFUSED them ... reads are PROVEN
#
#   CREDIT path: PARTLY exercised ... the reader returned no credits WITHOUT decoding
#   an amount -- its uiTokenAmount/balance-delta reads are still unproven
#
# Both computed honestly from what each could see. `observed` is filled in by
# check_address, which runs BEFORE the targeted step, so the headline conclusion of the
# run was built from an observation taken before the thing that settled it. Same defect
# CreditPathObserved was created to stop, in the one direction it did not cover.
# ---------------------------------------------------------------------------


def test_the_targeted_read_decoding_an_amount_reaches_the_summary():
    """MUTATION: discard prove_the_spl_reader()'s return value -- which is what main() did --
    and the summary says "still unproven" about a reader the same run just proved.
    """
    scan_found_nothing = CreditPathObserved(address_read=True, is_spl=True, signatures=5,
                                            credits=0, refused=0)
    assert not scan_found_nothing.decoded_an_amount
    assert "still unproven" in summary_text(scan_found_nothing), "the state before the step"

    after = scan_found_nothing._replace(targeted_read_decoded=True)
    assert after.decoded_an_amount, (
        "a refused credit counts -- the amount is decoded before the memo check, the same rule "
        "this property already applies to the scan's counts"
    )
    text = summary_text(after)
    assert "still unproven" not in text
    assert "DECODED a real amount from the TARGETED read" in text, (
        "and it says WHICH read got there: the scan proves the reader over a window, this "
        "proves it over one transaction chosen for carrying a credit"
    )
    assert "0 credited, 0 refused" not in text, (
        "the scan's counts are zero in this case, so reporting them would say the opposite of "
        "what happened"
    )
    assert "5 signature(s) in the scan's own window decoded nothing" in text, (
        "the window's result is still reported, as a fact about the window"
    )


def test_prove_the_spl_reader_hands_back_what_it_established():
    """The observation, not a flag -- same reason CreditPathObserved exists."""
    _revealed_by.clear()
    _revealed_by[_A_HOLDER] = RevealingTx("65bWBunzbNMkN9d5", credits_owner=True)
    before = CreditPathObserved(address_read=True, is_spl=True, signatures=5,
                                credits=0, refused=0)

    decoded = solana_chain_check.prove_the_spl_reader(
        _seeded_adapter({"getTransaction": _spl_transaction("0", "2500000000")}, _A_MINT),
        _A_HOLDER, make_runner([]), before)
    assert decoded.targeted_read_decoded is True, "a refused credit IS a decode"

    nothing = solana_chain_check.prove_the_spl_reader(
        _seeded_adapter({"getTransaction": _spl_transaction("5", "5")}, _A_MINT),
        _A_HOLDER, make_runner([]), before)
    assert nothing.targeted_read_decoded is False, "no positive delta, so nothing was decoded"

    _revealed_by.clear()
    untouched = solana_chain_check.prove_the_spl_reader(
        _seeded_adapter({}, _A_MINT), "rTYPED", make_runner([]), before)
    assert untouched == before, "the step never ran, so the observation is unchanged"


def test_a_step_that_RAISED_is_not_counted_as_having_decoded():
    """`any([])` is False, and that is the right answer: a step that died proved nothing.

    MUTATION: default the outcome to True when the list is empty and a run whose targeted read
    crashed reports the reader as proven.
    """
    _revealed_by.clear()
    _revealed_by[_A_HOLDER] = RevealingTx("65bWBunzbNMkN9d5", credits_owner=True)
    before = CreditPathObserved(address_read=True, is_spl=True, signatures=5,
                                credits=0, refused=0)
    failures = []

    def explode(_method, *_params):
        raise chains_solana.SolanaRPCError("the endpoint went away")

    adapter = _seeded_adapter({}, _A_MINT)
    adapter.call = explode
    after = solana_chain_check.prove_the_spl_reader(adapter, _A_HOLDER,
                                                    make_runner(failures), before)
    assert failures, "the step failed and is counted as a failure"
    assert after.targeted_read_decoded is False
    assert "still unproven" in summary_text(after)


def test_the_proof_step_captures_the_adapters_warning_instead_of_letting_it_escape(capsys):
    """It printed at column 0 between the step's announcement and its result.

    From the operator's run, and it is the same three lines _CapturedAdapterLogs' docstring
    already quotes from 2026-09-30, one step over:

        _spl_credits over 5MVQ2U12Y8NcdV7y... ...  <- the one transaction known to CREDIT ...
    SOL deposit 5MVQ2U12Y8NcdV7yD9bc... CANNOT BE ATTRIBUTED and was NOT credited: ...
        ok   DECODED 1 credit(s) and then REFUSED them: ...

    The handler existed and was correct; only one of the two readers used it (rule 8).

    MUTATION: drop the `with _capturing_adapter_logs()` and the record reaches the root
    handler, unindented, mid-step.
    """
    adapter = _seeded_adapter({"getTransaction": _spl_transaction("0", "2500000000")}, _A_MINT)
    capsys.readouterr()
    line = _spl_reader_line(adapter, _A_HOLDER, "65bWBunzbNMkN9d5")
    escaped = capsys.readouterr()

    assert "PROVEN" in line, "the step's own result is unchanged"
    assert "logged:" in line, "the adapter's record is folded INTO the step, indented"
    assert "CANNOT BE ATTRIBUTED" not in escaped.out + escaped.err, (
        "and reaches no handler of its own -- a line at column 0 mid-step breaks the block the "
        "operator pastes back"
    )


def test_main_carries_the_targeted_proof_into_the_summary(monkeypatch, capsys):
    """END TO END, because the unit tests all passed with main() throwing the result away.

    `observed = prove_the_spl_reader(...)` -> `prove_the_spl_reader(...)` SURVIVED every unit
    test above: the function returned the right observation and nothing read it. That is the
    fourth main()-level mutation in this session to survive for exactly that reason, and the
    only thing that catches it is driving main() and reading the summary.

    THE SEEDED CLUSTER IS THE OPERATOR'S SITUATION, not a convenient one: the owner's own
    recent window holds a transaction that does not credit it, and the transaction that DOES
    credit it is reachable only through the mint's traffic. That is why the targeted step
    exists, and it is the shape four devnet runs kept landing in.
    """
    _revealed_by.clear()
    target, in_the_window = "65bWBunzbNMkN9d5", "4yPFj1mqTVnxbKHd"
    holder = _A_HOLDER

    class SplitCluster(_WholeClusterStub):
        def __call__(self, url, data=None, **kwargs):
            payload = _json.loads(data) if data else {}
            method, params = payload.get("method", ""), payload.get("params") or []
            if method == "getTokenLargestAccounts":
                return _Throttled()
            if method == "getSignaturesForAddress":
                # THE MINT'S LISTING AND THE OWNER'S ARE DIFFERENT, which is the whole premise.
                listed = target if params and params[0] == _A_MINT else in_the_window
                return _Ok([{"signature": listed, "confirmationStatus": "finalized"}])
            if method == "getTransaction":
                if params and params[0] == target:
                    # Credits the owner, no memo -> DECODED then refused, which is a proof.
                    return _Ok(_a_crediting_mint_transaction(owner=holder))
                # In the owner's window and carrying nothing for it: the scan decodes nothing.
                return _Ok(_a_crediting_mint_transaction(owner="rSOMEONEELSE"))
            if method == "getAccountInfo":
                return _Ok({"value": {"data": {"parsed": {"info": {"decimals": 9,
                                                                   "owner": holder}}},
                                      "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}})
            return super().__call__(url, data=data, **kwargs)

    monkeypatch.setattr(solana_chain_check, "RPC_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(
        solana_chain_check.Config, "RPC",
        {**solana_chain_check.Config.RPC,
         "SOL": {**solana_chain_check.Config.RPC["SOL"], "url": "http://127.0.0.1:1",
                 "mint": _A_MINT}})
    monkeypatch.setattr(chains_solana.requests, "post", SplitCluster())
    monkeypatch.setattr("sys.argv", ["solana_chain_check.py", "--mint", _A_MINT,
                                     "--find-holder"])
    solana_chain_check.main()
    out = text_of(capsys.readouterr().out)

    assert f"_spl_credits over {target}" in out, "the targeted step ran -- the scan found nothing"
    assert "reads are PROVEN" in out, "and it decoded an amount, then refused it for no memo"
    assert "DECODED a real amount from the TARGETED read" in out, (
        "AND THE SUMMARY SAYS SO. This is the assertion the mutation breaks: with the return "
        "value discarded the summary reads 'still unproven' four lines under 'PROVEN'."
    )
    assert "still unproven" not in out


def test_a_step_line_claims_nothing_about_readers_it_did_not_run():
    """From the operator's eighth run, and the FOURTH instance of one pattern in this file.

    The line read:

        ... reads are PROVEN. That is the last reader in this adapter with no live evidence.

    Two defects in one sentence. It denies what the clause in front of it just established --
    the reader cannot both be proven and have no live evidence -- and "the last reader in this
    adapter" is a superlative over every reader across every run, asserted by a step that read
    one transaction.

    The three before it: the summary's "has not decoded one" about a reader the run never
    called; the summary built from an observation taken before the step that settled it; the
    caption promising "a balance" over a transaction the selection had only checked for an
    entry. Same shape every time, so this pins the rule rather than the instance -- a step
    reports its own read, and cross-cutting conclusions belong to the summary, where
    CreditPathObserved is the authority on what ran.

    MUTATION: put any of those phrases back and this fails on whichever outcome carries it.
    """
    outcomes = {
        "attributed": _spl_transaction("0", "2500000000", memo=True),
        "refused": _spl_transaction("0", "2500000000"),
        "no delta": _spl_transaction("5", "5"),
    }
    for name, response in outcomes.items():
        line = _spl_reader_line(_seeded_adapter({"getTransaction": response}, _A_MINT),
                                _A_HOLDER, "65bWBunzbNMkN9d5")
        for claim in ("last reader", "no live evidence", "has not decoded",
                      "still unproven", "_native_credits"):
            assert claim not in line, (
                f"the {name} outcome claims '{claim}', which is about readers or runs this "
                f"step did not touch"
            )
        if "PROVEN" in line:
            assert "no live evidence" not in line, (
                "and above all it may not deny in one clause what it established in the last"
            )

    # THE SUMMARY IS WHERE THAT BELONGS, and it still says it -- the rule moves the claim, it
    # does not delete it.
    assert "was NOT exercised by this run" in summary_text(
        CreditPathObserved(address_read=True, is_spl=True, signatures=5, credits=0, refused=0,
                           targeted_read_decoded=True))


def test_a_dropped_credits_AMOUNT_is_on_the_line_and_named_as_a_delta():
    """The number an operator needs first, and which quantity it is.

    The 2026-10-01 native run printed, four lines apart:

        balance ...  ok  28.7786992 SOL
        ... 1 credit(s) dropped, 28.7786992 SOL in total

    Two different quantities that happened to print the same number -- the transaction's
    balance DELTA and the account's whole balance. They are equal exactly when the dropped
    credit is the account's funding transaction, and nothing on the screen said which it was.
    The step's own per-drop line meanwhile listed the signature and the reason and no amount at
    all, which is the number that decides whether anyone goes looking.

    MUTATION: drop the amount from the line, or call it a total, and this fails.
    """
    line = _deposits_line(
        _DepositStub(drops=[_a_drop(credits=1, amount=28.7786992)], signatures_read=8),
        "rADDR", 10)
    assert "28.7786992 credited by this transaction" in line, (
        "the amount, named as a delta rather than as a total or a balance"
    )
    assert "in total" not in line, (
        "'in total' is what made it indistinguishable from the account's balance"
    )
    assert "1 credit(s)" in line, "and the count stays -- both numbers matter to a human"
