#!/usr/bin/env python3
"""Both swap runners are DRIVEN to completion here, and asked what they returned.

Role: test (behavioral verification of atomic_swap_xrp.run_xrp_first and
      run_chain_first, with every external call stubbed)
Reads: atomic_swap_xrp.py through the real functions
Writes: nothing. No file, no database, no socket.
Can move funds: no. Nothing here opens a connection to any chain, loads any key,
      or signs anything -- `submit_xrp` is a local function returning a canned
      tesSUCCESS, and the two script-leg calls are replaced before either runner
      starts.
Mainnet-safe: yes
Live-safe: yes

WHY THIS FILE EXISTS, AND IT IS THE SHARPEST GAP THE 2026-10-09 PYRIGHT PASS
FOUND.

    -    return False
         return True

run_xrp_first() returned False on its SUCCESS path, with `return True` sitting
unreachable one line below it. Both lines arrived together in a7ab61de
(2026-09-27) when the function gained `-> bool`. So the direction that has
actually run end to end -- OK=15 on 2026-09-26, per the module header -- reported
FAILURE on completion, for twelve days, while run_chain_first returned True at
the identical point and run_xrp_first's own docstring said "Returns True when the
swap completed."

NOTHING CAUGHT IT BECAUSE NOTHING RAN IT. Counted the same day: one test file
mentions the runners -- tests/test_swap_direction_parties.py -- and it parses
their AST rather than calling them, deliberately, because the defect IT exists
for is run_chain_first escrowing B->A while its docstring said A->B, which is a
property of the source. Not one test in the suite DROVE either runner. A return
value no caller reads and no test exercises is a value nobody can be wrong about
until the first person trusts it.

TWO SIGNALS, NOT ONE, AND THAT IS THE DESIGN RATHER THAN AN OVERSIGHT. main()
does `runner(ctx)` and DISCARDS the result, then returns console.summary(), which
is `1 if failures else 0`. So the process exit code comes from the CHECK TALLY
and the bool says "I completed the sequence". A runner can honestly return True
while a verification failed -- the sequence finished, the result was wrong -- and
test_a_failed_check_still_exits_nonzero_even_though_the_runner_returned_true
below pins exactly that, so nobody later "simplifies" one signal into the other
and loses the distinction rule 13 is about.
"""

from __future__ import annotations

import contextlib
import hashlib
import os

import pytest
from chains import wallet_lock
from chains.base import RPCAdapter
from chains.xrp_crypto_condition import preimage_condition
from modules import script_leg
from modules.script_leg import ScriptLegKeys
from network_target import UNCONFIGURED_PORT
from regtest.keys import generate_key

import atomic_swap_xrp as driver

#: The 32 bytes the whole swap is interlocked by. Generated per run rather than
#: fixed, because a hardcoded preimage in a repository is a preimage, and this
#: file's own header promises it holds no key material.
SECRET = os.urandom(32)
SECRET_HASH = hashlib.sha256(SECRET).digest()

#: WHAT GOES IN THE TWO `*_xrp_secret` SLOTS, AND IT IS NAMED RATHER THAN SPELLED.
#:
#: ruff's S106 fires on a string LITERAL handed to an argument whose name looks
#: like a credential, and it is right to look: an XRPL account secret in those
#: slots would be a key in a repository. The first draft wrote "sA" and "sB" inline
#: and took two S106s, and the available answers were a `noqa` claiming they are
#: harmless or a name saying so. The name is better -- a reader scanning this
#: fixture learns what the value is for without decoding a suppression, and the
#: linter keeps firing for anyone who later pastes a real one in.
#:
#: Nothing here signs. `submit_xrp` below is a local function returning a canned
#: tesSUCCESS and no socket is opened by this file, so the value is never used as
#: a secret by anything -- it only has to be a non-empty string.
NOT_A_SEED = "not-a-real-xrpl-secret"

#: A plausible XRPL sequence and a contract the script leg "funded". Neither is
#: real; both only have to be the SHAPE the runners destructure.
SEQUENCE = 21051271
CONTRACT = {"txid": "F" * 64, "p2shAddress": "2NQ", "vout": 0, "redeemScript": b"\x51"}

#: The final banner each runner prints once it has done everything. Asserting on
#: it is what stops a runner that returns True EARLY from passing: `is True` alone
#: is satisfied by `def run(ctx): return True`.
FINAL_BANNER = "== WHAT CHANGED HANDS =="


class RecordingConsole:
    """A console that records instead of printing. Satisfies step_console.StepReporter.

    NOT step_console.Console AND NOT A SUBCLASS OF IT, deliberately. The point of
    a recorder is to fail loudly the day a runner reaches for something the stub
    does not have; inheriting would have the shipped Console answer instead,
    silently. See step_console.StepNarrator's docstring, which argues this at
    length for the protocols added the same day.
    """

    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []
        self.lines: list[str] = []

    def step(self, number: int, title: str) -> None:
        self.lines.append(f"step {number} {title}")

    def say(self, text: str) -> None:
        self.lines.append(text)

    def banner(self, text: str) -> None:
        self.lines.append(f"== {text} ==")

    def check(self, label: str, got: object, expected: object, ok: bool) -> bool:
        self.results.append((label, ok))
        return ok

    def summary(self) -> int:
        """The real Console's arithmetic, which is what main() returns as an exit code."""
        return 1 if [label for label, ok in self.results if not ok] else 0

    def elapsed(self) -> str:
        """A fixed string: this recorder is not a stopwatch and must not become one.

        The real Console returns a microfortnight figure from time.monotonic(). A
        recorder returning a REAL elapsed time would put a different value in every
        run's output, and the one thing a test asserting on `console.lines` must not
        have is a line that changes between runs.
        """
        return "0.0\u00b5fn (0.0s)"

    @property
    def failures(self) -> list[str]:
        return [label for label, ok in self.results if not ok]


@contextlib.contextmanager
def _no_unlock(*_args, **_kwargs):
    """chains.wallet_lock.unlocked_for_payout, with no wallet to unlock."""
    yield


def _an_adapter_nothing_calls() -> RPCAdapter:
    """A real RPCAdapter on the UNCONFIGURED port, because nothing here calls it.

    `ctx.grc` is never dereferenced by either runner -- grepped: there is no
    `ctx.grc.<member>` anywhere in them. It is only HANDED to
    `unlocked_for_payout()` and `claim_scriptsig_hex()`, both of which this file
    replaces before either runner starts. So what it needs to be is the declared
    type, not a working connection.

    A REAL ONE RATHER THAN A STUB, for the reason this whole session kept finding:
    a stub would be a second description of an adapter, and the only thing that
    makes this one safe is that it is the real class with no port. Constructing it
    opens nothing -- RPCAdapter does no I/O in __init__ -- and
    network_target.UNCONFIGURED_PORT is 0, which is the value config.py defaults
    every chain to precisely so that an unconfigured chain refuses rather than
    guessing. If a later change makes a runner actually call it, the failure is a
    connection refusal naming port 0, not a silent stub answering.
    """
    return RPCAdapter(user="", password="", host="127.0.0.1", port=UNCONFIGURED_PORT,
                      wallet="", timeout=1.0)


def _expected_rise(runner_name: str) -> int:
    """How many drops B's balance must gain, WHICH IS NOT THE SAME IN BOTH DIRECTIONS.

    This is the file's one real asymmetry and the driver's own comment explains it:
    in the chain-first direction B submits the EscrowFinish, so the fee leaves the
    same account the escrow pays INTO and the net rise is the escrowed amount MINUS
    that fee. In the XRP-first direction the CREATOR submits the finish, so the fee
    leaves a different account and the rise is the full amount.

    Measured on the first grc-first run: a 1,000,000-drop escrow and a 360-drop fee.
    Asserting `+XRP_DROPS` in both places was one expectation covering two different
    arithmetics, and the wrong half of it reported a completed swap as a failure --
    which is the instrument losing its reader (rule 14).

    COMPUTED THROUGH THE REAL finish_fee_drops() RATHER THAN SPELLED AS 360, so a
    change to the fee rule moves the production code and this fixture together. A
    literal here would be a second fee schedule, which is the defect
    static/script.js's header spends a paragraph on.
    """
    if runner_name == "run_chain_first":
        return driver.XRP_DROPS - driver.finish_fee_drops(driver.preimage_fulfillment(SECRET))
    return driver.XRP_DROPS


def _rising_balance(rise: int):
    """balance_drops(), answering 0 first and `rise` after.

    BOTH RUNNERS CALL IT TWICE -- once before the escrow is finished and once
    after -- and check the difference. A stub returning a constant makes that check
    FAIL, which is how the first run of this harness reported 6 OK and 1 FAIL and
    nearly got written up as a defect in the runner. The rise IS the thing under
    test on that line, so the stub has to produce one.
    """
    seen = {"calls": 0}

    def balance_drops(_address: str) -> int:
        seen["calls"] += 1
        return 0 if seen["calls"] == 1 else rise

    return balance_drops


@pytest.fixture
def driven(monkeypatch):
    """A factory: call it with the runner's name, get back (console, context).

    A FACTORY RATHER THAN A PLAIN FIXTURE because the balance stub has to know
    which direction it is standing in for -- see _expected_rise(). Discovered by
    running it: the first version used one rise for both and run_chain_first failed
    two checks, which read like a defect in the runner and was a defect in the stub.
    """
    def build(runner_name: str):
        monkeypatch.setattr(driver, "balance_drops", _rising_balance(_expected_rise(runner_name)))
        # A NON-EMPTY DICT, AND THE EMPTY ONE COST A DEBUGGING ROUND. run_chain_first
        # does `source = validated if attempt == 1 and validated else None` -- so a
        # falsy `{}` makes it fall through to a REAL `rpc("tx", ...)` network call,
        # which raises, is caught by the loop's broad except, and leaves `revealed`
        # None after every attempt. The runner then correctly reports that it could
        # not recover the secret, and the stub for preimage_from_escrow_finish below
        # is never reached at all. A stub that is never called looks exactly like a
        # stub that returned the wrong thing.
        monkeypatch.setattr(driver, "wait_validated", lambda _console, _hash: {"validated": True})
        monkeypatch.setattr(driver, "claim_scriptsig_hex", lambda _adapter, _txid: ("00", []))
        monkeypatch.setattr(driver, "preimage_from_scriptsig", lambda _sig, _commitment: SECRET)
        # THE TWO DIRECTIONS READ THE SECRET FROM DIFFERENT LEDGERS, which is the
        # whole point of the protocol and therefore needs two stubs. XRP-first: A
        # reads it out of the script leg's scriptSig (above). Chain-first: A reads
        # it off the XRP ledger, out of B's own EscrowFinish Fulfillment field.
        # Stubbing only the first left `revealed` None in run_chain_first, which
        # returns False by design and named the right cause in its own output.
        monkeypatch.setattr(driver, "preimage_from_escrow_finish", lambda _source, _hash: SECRET)
        monkeypatch.setattr(wallet_lock, "unlocked_for_payout", _no_unlock)
        monkeypatch.setattr(script_leg, "fund_the_script_leg", lambda *_a, **_k: CONTRACT)
        monkeypatch.setattr(script_leg, "claim_the_script_leg", lambda *_a, **_k: "C" * 64)

        def submit_xrp(_tx_json, _secret):
            """tesSUCCESS for anything, carrying the Sequence and hash the runners read."""
            return {"engine_result": "tesSUCCESS", "engine_result_message": "ok",
                    "tx_json": {"Sequence": SEQUENCE, "hash": "A" * 64}}

        console = RecordingConsole()
        context = driver.SwapContext(
            console=console, chain="LTC", grc=_an_adapter_nothing_calls(), submit_xrp=submit_xrp,
            secret=SECRET, secret_hash=SECRET_HASH, condition=preimage_condition(SECRET),
            a_xrp="rA", a_xrp_secret=NOT_A_SEED, b_xrp="rB", b_xrp_secret=NOT_A_SEED,
            a_grc="LclaimA", b_grc="LrefundB",
            chain_amount=driver.Decimal("66.1"), chain_timeout=2_300_000,
            xrp_cancel_after=843784768, passphrase="", wallet_encrypted=False,
            script_client=object(),
            leg_keys=ScriptLegKeys(claim=generate_key(), refund=generate_key()),
        )
        return console, context

    return build




@pytest.mark.parametrize("runner_name", ["run_xrp_first", "run_chain_first"])
def test_a_completed_swap_reports_True(driven, runner_name):
    """THE REGRESSION. `is True`, not truthy, and the banner as the positive control.

    MUTATION: put `return False` back above run_xrp_first's `return True` -- which
    is what shipped between 2026-09-27 and 2026-10-09 -- and this fails for that
    runner while every check in it still passes, which is exactly how the defect
    looked from the outside.

    `is True` rather than `assert returned`: the declared type is `bool`, and a
    runner that drifted to returning a truthy dict or a non-empty string would be
    a different function with the same tests passing.

    THE BANNER ASSERTION IS NOT DECORATION. `assert returned is True` on its own
    is satisfied by `def run_xrp_first(ctx): return True`, which does no swap at
    all -- the same "nothing was looked at and nothing was found are the same
    green" that cost three mutations elsewhere in this session. The banner is the
    last thing either runner prints, so reaching it means the sequence ran.
    """
    console, context = driven(runner_name)
    returned = getattr(driver, runner_name)(context)

    assert returned is True, (
        f"{runner_name}() completed the swap and reported {returned!r}. "
        f"Checks: {len(console.results)}, failures: {console.failures or '(none)'}"
    )
    assert FINAL_BANNER in console.lines, (
        f"{runner_name}() returned True without reaching its final banner, so it did not "
        f"run the sequence -- the lines it did print were: {console.lines[-3:]}"
    )
    assert console.results, "the runner recorded no checks at all; this assertion would be vacuous"
    assert console.failures == [], (
        f"{runner_name}() ran clean stubs and still failed a check: {console.failures}"
    )


@pytest.mark.parametrize("runner_name", ["run_xrp_first", "run_chain_first"])
def test_a_failed_check_still_exits_nonzero_even_though_the_runner_returned_true(driven, runner_name):
    """TWO SIGNALS, PINNED, so nobody later folds one into the other.

    main() discards the runner's bool and returns console.summary(). That is not
    redundancy: the bool says "I completed the sequence" and summary() says
    "every verification passed". A swap can finish with a verification wrong, and
    an operator needs those to read differently -- which is rule 13's "a cycle
    that did no work must not report the same way as one that did", applied to a
    cycle that did the work and got the wrong answer.

    Simulated by making the balance never rise, which is the single most likely
    real failure here: both transactions applied, and B is not holding the money.
    """
    console, context = driven(runner_name)
    context.console.check = _check_that_fails_the_balance(console)

    returned = getattr(driver, runner_name)(context)

    # THE POSITIVE CONTROL, and without it this test passes whenever ANY check
    # fails -- including one failing because a stub was wrong, which is how the
    # balance arithmetic and the falsy `wait_validated` were both caught while
    # writing this file. summary() == 1 is only evidence about the balance check
    # if the balance check is the one that failed.
    assert console.failures == [label for label, _ in console.results if "balance rose" in label], (
        f"the forced failure was meant to be the balance check alone; the run failed "
        f"{console.failures}"
    )
    assert returned is True, "the sequence still completed, so the runner still says so"
    assert console.summary() == 1, (
        "a failed verification must make the EXIT CODE non-zero even when the runner "
        "returned True -- that is the only signal main() passes to the shell"
    )


def test_an_unrecoverable_secret_makes_run_chain_first_return_False(driven, monkeypatch):
    """THE ASYMMETRY, found by writing this file and worth pinning rather than noting.

    The two runners do NOT treat a failed check alike, and that is correct rather
    than drift. A failed BALANCE check leaves both returning True -- the sequence
    ran, the verification disagreed. But run_chain_first has one check with an
    explicit `return False` under it, and it is the one that matters most:

        if revealed is None:
            console.check("the secret was recovered from the XRP ledger", ...)
            console.say("A cannot claim the {chain} ... -- except that B HAS
                         ALREADY TAKEN THE XRP. Read {finish_hash} by hand;
                         the secret is in its Fulfillment field.")
            return False

    That is the genuinely dangerous state in this direction: B holds the XRP and A
    cannot take the chain leg, so the swap is half-done against A. The runner
    refuses to call that a completion, and it tells the operator exactly where the
    secret is. This test is here so a later tidy that made every path return True
    "for consistency" fails instead.

    Driven by making the ledger read find no matching Fulfillment, which is what
    the real loop does after READ_ATTEMPTS misses.
    """
    # ORDER MATTERS AND GETTING IT WRONG LOOKS LIKE A PASSING SWAP. `driven(...)`
    # applies the fixture's own stubs when it is CALLED, so patching
    # preimage_from_escrow_finish before that line is undone by it -- the first
    # draft did exactly that and this test reported `True is False` against a
    # runner behaving correctly. Build the context first, then override.
    console, context = driven("run_chain_first")
    monkeypatch.setattr(driver, "preimage_from_escrow_finish", lambda _source, _hash: None)
    monkeypatch.setattr(driver, "READ_ATTEMPTS", 1)
    monkeypatch.setattr(driver, "READ_POLL_SECONDS", 0)

    assert driver.run_chain_first(context) is False, (
        "a swap where B took the XRP and A cannot claim the chain leg is not a completion"
    )
    assert "the secret was recovered from the XRP ledger" in console.failures
    assert any("HAS ALREADY TAKEN THE XRP" in line for line in console.lines), (
        "the operator must be told which side is exposed, not just that something failed"
    )
    assert any("Fulfillment field" in line for line in console.lines), (
        "and where to find the secret by hand -- rule 14: say what to do about it"
    )


def _check_that_fails_the_balance(console: RecordingConsole):
    """console.check, with the balance assertion forced to fail and nothing else."""
    def check(label: str, got: object, expected: object, ok: bool) -> bool:
        if "balance rose" in label:
            ok = False
        console.results.append((label, ok))
        return ok
    return check
