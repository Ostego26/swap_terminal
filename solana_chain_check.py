#!/usr/bin/env python3
"""Prove the Solana adapter against a real cluster. Read-only; broadcasts nothing.

Role: entry point (operator diagnostic for the Solana adapter)
Reads: a Solana JSON-RPC endpoint -- getHealth, getVersion, getGenesisHash,
       getSlot, getEpochInfo, getBalance, getAccountInfo,
       getTokenAccountBalance, getMinimumBalanceForRentExemption,
       getSignaturesForAddress, getTransaction. All reads.
Writes: stdout only. No file, no database, no chain.
Can move funds: NO, and it cannot be made to. It imports chains/solana.py,
       which holds no keypair and cannot sign, and it never calls
       send_to_address(). There is no flag that makes this broadcast.
Mainnet-safe: yes to run. It PRINTS which network it is on before doing
       anything else, because a line that does not say so will eventually be
       read as the wrong one.

WHY THIS FILE EXISTS.

Nothing in the Solana path has ever run against a cluster. Measured
2026-09-25 from the environment it was written in: api.devnet.solana.com and
release.anza.xyz both return 403 from the proxy, so no endpoint was reachable
and `solana-test-validator` could not be installed -- its only distribution
channels are those two hosts and github.com, all denied. The RPC method names,
parameter shapes and response field names were written from Solana's
documentation and from the shapes grc-sol-swap/abstergo_exchange/server.js
already relies on.

**So if one of those field names is wrong, the whole test suite still passes
and the adapter fails on the first real call.** This script is the thing that
would show that false (CLAUDE.md rule 17), and running it is the proof --
which makes the OPERATOR'S RUN the proof, exactly as regtest_htlc_verify.py is
for the HTLC path.

It is at the repository root for rule 10's reason: an operator runs it, so it
belongs where it can be found without knowing the layout.

RULE 14 GOVERNS EVERY LINE IT PRINTS. Each step announces BEFORE it runs, not
only after; every step carries its own elapsed time in microfortnights with
the seconds in parentheses; an empty result prints `(none)` rather than a blank
gap; and each number is printed beside what it means, because the operator
reads the screen and not this source.

USAGE (all read-only):

    source .venv/bin/activate
    export SOL_RPC_URL=http://127.0.0.1:8899          # a local test validator
    python3 solana_chain_check.py

    # or against devnet, and it will say DEVNET in the banner:
    export SOL_RPC_URL=https://api.devnet.solana.com
    python3 solana_chain_check.py --address <a wallet> --mint <an SPL mint>

Exit code is 0 when every step answered, 1 when any step failed. A failure is
printed with the method that failed and what was expected, so the paste back is
self-describing.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import textwrap
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple, Protocol, TypedDict

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# All five imports are reachable only because of the sys.path line above;
# hoisting them would break the import it enables. That is what the E402
# suppressions claim and what a reader can check from these lines.
from chains.solana import SolanaAdapter, SolanaRPCError, UnattributableCredit  # noqa: E402
from chains.solana_address import (  # noqa: E402
    SOLANA_DEVNET_ACCOUNT,
    describe_address,
    is_valid_address,
)
from chains.solana_memo import (  # noqa: E402
    MEASURED_MEMO_PROGRAM_IDS,
    MEMO_PROGRAM_IDS,
    deposit_tag_from,
    memo_strings_in,
)
from chains.solana_units import (  # noqa: E402
    ACCOUNT_STORAGE_OVERHEAD_BYTES,
    BALANCE_COMMITMENT,
    DISCOVERY_COMMITMENT,
    FINALIZED_RANK,
    LAMPORTS_PER_BYTE_FOR_RENT_EXEMPTION,
    RENT_EXEMPT_SYSTEM_ACCOUNT_LAMPORTS,
    RENT_EXEMPT_TOKEN_ACCOUNT_LAMPORTS,
    SYSTEM_ACCOUNT_SPACE,
    TOKEN_ACCOUNT_SPACE,
    describe_commitment,
)
from config import Config  # noqa: E402
from microfortnights import format_duration  # noqa: E402
from network_target import solana_cluster  # noqa: E402

# The genesis hashes that identify Solana's three public clusters. A cluster
# cannot lie about this and it does not depend on the URL's hostname, which is
# why the network is IDENTIFIED rather than inferred from the endpoint: an
# operator pointing a "devnet" alias at mainnet would otherwise read the word
# devnet in this banner all the way to a mainnet transfer.
#
# THE TABLE MOVED to network_target.py on 2026-10-01 and is re-exported here under
# its original name, because swap_readiness.py needed the same three hashes and the
# only ways to get them were to import this root entry point from another one
# (rule 10 inverted -- a file importing a file) or to write them out twice (rule
# 8's defect, and a quiet one: two copies of a hash table agree until a cluster is
# added to one). The name is kept so every reference here and in
# tests/test_solana_chain_check_units.py keeps working and keeps testing the one
# table that now exists.


class StepFailed(Exception):
    """One check could not be completed. Carries the message already formatted."""


def step(label: str, expectation: str = ""):
    """Announce a step before it runs, and time it. Returns a closure to finish it.

    Rule 14's first clause -- "announce before, not only after" -- is the whole
    reason this is a two-part helper rather than a decorator that prints on
    return. A line that only appears on completion is invisible during the
    wait, which is exactly when the operator is deciding whether to Ctrl-C.
    """
    suffix = f"  <- {expectation}" if expectation else ""
    print(f"  {label} ...{suffix}", flush=True)
    started = time.monotonic()

    def done(result_line: str, ok: bool = True) -> bool:
        marker = "ok  " if ok else "FAIL"
        print(f"    {marker} {result_line}   [{format_duration(time.monotonic() - started)}]", flush=True)
        return ok

    return done


def make_runner(failures: list[str]):
    """Return a run(label, expectation, fn) that announces, times, and records.

    Extracted so that each phase below is a short function rather than another
    forty lines inside main(). CLAUDE.md rule 12 on C901: "a main() past the
    ceiling is orchestration that has swallowed decisions... the fix is to
    extract the decision so it can be called with seeded inputs, not to raise
    the ceiling."
    """
    def run(label: str, expectation: str, fn):
        done = step(label, expectation)
        try:
            line = fn()
        except SolanaRPCError as exc:
            failures.append(label)
            done(f"{exc}", ok=False)
        except Exception as exc:  # noqa: BLE001 -- checked: this is a DIAGNOSTIC and one broken step must not hide the other nine. The failure is NOT swallowed -- it is named, printed, counted into `failures`, and turned into a non-zero exit code, so no caller can read it as a pass.
            failures.append(label)
            done(f"unexpected {type(exc).__name__}: {exc}", ok=False)
        else:
            done(line)

    return run


class BannerSettings(TypedDict):
    """The three settings print_banner() reads, and the reason it has its own shape.

    IT WAS `rpc: dict` AND THAT COST TWO KeyErrors IN ONE TEST FILE, which
    tests/test_solana_chain_check_units.py records at its own call site:

        Built one by hand earlier in this file and print_banner raised KeyError on
        'mint'; it raised again here on 'min_commitment_rank'. A hand-built config
        tests the hand-built config.

    A bare `dict` parameter says "any dict", so a caller that assembles one by hand
    -- a test, an operator tool, a future caller with a URL and nothing else -- gets
    a KeyError from inside the banner, after the first line has already printed.
    Three required keys is the actual contract and now the declared one, so a short
    dict fails where it is written.

    A SUBSET OF config.SolanaRpc RATHER THAN SolanaRpc ITSELF, deliberately. The
    full six-field entry is assignable to this (a TypedDict may carry extra keys),
    so main() passes its copy of Config.RPC["SOL"] unchanged -- while a caller with
    only these three is still legal, which is what the seeded banner tests are and
    what keeps this function testable without a whole chain configuration.
    """

    url: str
    mint: str
    min_commitment_rank: int


def print_banner(rpc: BannerSettings, address: str, address_why: str = "") -> None:
    """Everything that decides the answer, before anything runs (rule 14).

    `address_why` says WHERE the address came from -- typed, configured, or defaulted. A default
    that appears without saying so is a default an operator reads as their own configuration.
    """
    print("solana_chain_check: READ-ONLY. It signs nothing and broadcasts nothing.", flush=True)
    print(f"  endpoint        {rpc['url'] or '(SOL_RPC_URL is UNSET -- nothing can be checked)'}", flush=True)
    print(f"  mint            {rpc['mint'] or '(none -- checking native SOL)'}", flush=True)
    print(f"  address         {address or '(none)'}", flush=True)
    if address_why:
        print(f"                  <- {address_why}", flush=True)
    print(f"  threshold       rank {rpc['min_commitment_rank']}  <- a RUNG on the commitment ladder, NOT blocks", flush=True)
    print(f"  database        {Config.DB_PATH}  <- echoed for the paste; this script does not open it", flush=True)


class RpcCaller(Protocol):
    """Anything that can make ONE JSON-RPC call and hand the result back.

    WHY A PROTOCOL AND NOT SolanaAdapter. The functions below that take this
    touch exactly one member of the adapter -- `call` -- and annotating them
    `SolanaAdapter` was a promise none of them keeps: it said "give me the whole
    adapter" when what they need is a transport, and it made the signature
    unreadable as a statement of what the function reaches for. It also made
    every test that drives them unannotatable: tests/test_solana_chain_check_units.py
    stands in two classes that implement `call` and nothing else -- FakeAdapter
    (9 lines, measured 2026-10-09), whose own docstring says "Only what
    _network_line touches. Nothing here opens a socket.", and ScriptedAdapter
    (20 lines). Both say in prose precisely what this Protocol says in types,
    and until now the type checker had nothing to compare that claim against:
    it refused both stubs at all 28 call sites instead.

    A STUB THAT CARRIES ONLY WHAT IS NEEDED IS THE POINT, NOT A SHORTCUT. If one
    of these functions grows a second adapter call, the stub fails loudly on the
    attribute and this Protocol stops matching -- which is the cheap outcome. The
    expensive one is a stub that inherits the real adapter and quietly answers
    from the shipped code a test believed it had replaced.
    """

    def call(self, method: str, *params) -> Any: ...


class SignatureCoverage(Protocol):
    """The two counts coverage_clause() reconciles. Split out because it needs ONLY these.

    DepositReader below is this plus three more, and coverage_clause() takes
    THIS one. Annotating it with the wider Protocol would have been the same
    over-promise this round exists to remove, one level down: measured
    2026-10-09, coverage_clause()'s body touches `signatures_listed` and
    `signatures_read` and nothing else, so anything else in its parameter type
    is a demand it does not make. _deposits_line() still passes its own adapter
    straight through, because DepositReader derives from this.

    DERIVED RATHER THAN COPIED (rule 8). The two members are declared once, here,
    and DepositReader inherits them -- two Protocols each spelling
    `signatures_read` would agree on the day they were written and drift after.
    """

    @property
    def signatures_listed(self) -> int: ...

    @property
    def signatures_read(self) -> int: ...


class DepositReader(SignatureCoverage, Protocol):
    """What _deposits_line() reads off the adapter, and nothing else.

    FIVE MEMBERS -- the three declared below, plus SignatureCoverage's two,
    because _deposits_line() calls coverage_clause() with the same adapter. The
    reason they are LISTED rather than inherited FROM SolanaAdapter is in
    tests/test_solana_chain_check_units.py::_DepositStub's own comment: "THE STUB
    HAS TO CARRY EVERY ATTRIBUTE THE REAL ADAPTER EXPOSES, and this one was added
    on 2026-10-01 -- a stub missing a new attribute fails loudly here, which is
    the cheap outcome." That is a contract, it was being maintained by hand, and
    this is it written down where a checker can hold it.

    ALL FOUR DATA MEMBERS ARE READ-ONLY PROPERTIES -- the two here and
    SignatureCoverage's two -- AND THAT IS NOT STYLE. A
    plain class attribute satisfies a read-only property in a Protocol; the
    reverse is not true, and TWO of these break if written the other way.
    Measured 2026-10-09 by declaring each as a settable attribute and re-running
    pyright over this file and tests/test_solana_chain_check_units.py:

        signatures_read         3 errors. It IS a @property on SolanaAdapter
                                (listed minus the ones getTransaction could not
                                fetch), and a settable member cannot be
                                satisfied by a read-only one -- so the REAL
                                adapter would fail to match its own diagnostic's
                                parameter type, at the _deposits_line() call in
                                check_address().
        unattributable_drops    21 errors. The adapter holds it as
                                `list[UnattributableCredit]`, and a MUTABLE
                                member is invariant: `list[X]` does not satisfy
                                a settable `Sequence[X]`. Read-only makes it
                                covariant, which is also the true claim --
                                neither function here writes to it.

    signatures_listed and min_commitment_rank are plain attributes on the
    adapter and would survive either spelling. They are properties because
    read-only is what these two functions actually need, and because one
    exception in a list of four is a thing a reader has to stop and explain.

    THE `/` ON find_deposits_to_address IS LOAD-BEARING, measured the same way:
    17 errors without it, because _DepositStub names that parameter `_address`
    and pyright requires a named positional's SPELLING to match. The one call
    site here passes it positionally, so positional-only is both the fix and the
    truer claim. The `*` before tx_limit is not strictly required -- 0 errors
    without it -- but the call site passes tx_limit by keyword and nothing here
    passes it positionally. The `= ...` is required in the other direction: an
    implementation with no default for tx_limit does NOT satisfy this (probed
    2026-10-09), which is correct, since the adapter and both stubs have one.
    """

    @property
    def min_commitment_rank(self) -> int: ...

    @property
    def unattributable_drops(self) -> Sequence[UnattributableCredit]: ...

    def find_deposits_to_address(self, address: str, /, *, tx_limit: int = ...) -> list[dict]: ...


def check_cluster(adapter: SolanaAdapter, run) -> None:
    print("\nCLUSTER", flush=True)
    run("getHealth", "expect 'ok'; anything else means the node is behind or unwell",
        lambda: str(adapter.call("getHealth")))
    run("getVersion", "the solana-core build this endpoint runs",
        lambda: str(adapter.call("getVersion").get("solana-core", "(field absent -- the response shape is not what was expected)")))
    run("getGenesisHash", "IDENTIFIES THE NETWORK. A cluster cannot lie about this; a hostname can.",
        lambda: _network_line(adapter))
    run("getSlot", "the current slot; a DIAGNOSTIC, never a confirmation count",
        lambda: f"slot={adapter.get_slot()}")
    run("getEpochInfo", "epoch and slot index, for context when a deposit looks stalled",
        lambda: _epoch_line(adapter))


def check_rent(adapter: SolanaAdapter, run) -> None:
    # Rule 14's "state what the number means, next to the number", and here the
    # meaning is WHICH CLUSTER the reference describes. The 2026-09-26 devnet
    # run printed "DIFFERS from the reference 890880" with no cluster named, so
    # the operator could not tell a devnet-vs-mainnet difference (which would be
    # expected) from a value stale everywhere (which is what it turned out to
    # be). It says so now, in the header and in every mismatch line.
    #
    # AND THE HEADER SAID MORE THAN ANYBODY HAD MEASURED, corrected 2026-09-30.
    # It read "mainnet-beta, devnet and testnet all reached it on SIMD-0437 step
    # 2" -- stated as fact, in the line an operator reads on every run, while
    # chains/solana_units.py and the mismatch line below both mark the same
    # figure as MEASURED on devnet and SOURCED for the other two. Rule 17 is
    # exactly that those must not share a voice, and the version that hedges is
    # the correct one: no mainnet-beta or testnet endpoint has ever been read
    # from this tree.
    #
    # WHAT THE 2026-09-30 DEVNET RUN ADDS, because it bears on the next step
    # rather than on this one: that cluster answers getVersion 4.3.0, at slot
    # 506004644 / epoch 1171, and returned 650240 and 1488440 lamports -- 5080
    # exactly, at both sizes. solana_units.py records that SIMD-0437's steps 3-5
    # wait for Agave 4.4, so a cluster on 4.3.0 still carrying 5080 is the
    # consistent reading rather than a coincidence.
    print(
        f"\nRENT  (reference = {LAMPORTS_PER_BYTE_FOR_RENT_EXEMPTION} lamports/byte, which is SIMD-0437 step 2 -- "
        "ONE cluster parameter rather than a per-cluster figure, so a mismatch means a further step activated and "
        "is EXPECTED, not a defect. MEASURED on devnet; for mainnet-beta and testnet it is SOURCED from "
        "solana.com/upgrades/reduced-rent and has never been read from an endpoint here. The chain is the "
        "authority and nothing sizes a transfer from the constant.)",
        flush=True,
    )
    run("getMinimumBalanceForRentExemption(0)", f"expected {RENT_EXEMPT_SYSTEM_ACCOUNT_LAMPORTS} lamports for a system account (0 bytes)",
        lambda: _rent_line(adapter, SYSTEM_ACCOUNT_SPACE, RENT_EXEMPT_SYSTEM_ACCOUNT_LAMPORTS))
    run("getMinimumBalanceForRentExemption(165)", f"expected {RENT_EXEMPT_TOKEN_ACCOUNT_LAMPORTS} lamports for an SPL token account (165 bytes)",
        lambda: _rent_line(adapter, TOKEN_ACCOUNT_SPACE, RENT_EXEMPT_TOKEN_ACCOUNT_LAMPORTS))


def check_mint(adapter: SolanaAdapter, run) -> None:
    if not adapter.is_spl:
        print("\nMINT  (none configured -- native SOL. This is a RESULT, not a skipped step.)", flush=True)
        return
    print("\nMINT", flush=True)
    run("getAccountInfo(mint).owner", "which token program owns it; Token-2022 derives a DIFFERENT token account",
        adapter.token_program_id_or_default)
    run("getAccountInfo(mint).decimals", "the mint's OWN decimals. Never SOL's 9 (rule 11).",
        lambda: f"decimals={adapter.mint_decimals()}")


def check_address(adapter: SolanaAdapter, address: str, limit: int, run) -> CreditPathObserved:
    """Run the address section and RETURN what it observed, for the summary to report.

    THE RETURN VALUE IS THE POINT. main() used to hand print_summary() the two flags it had
    parsed, and the summary inferred coverage from them -- which was wrong three times running,
    because what a run REQUESTS and what it EXECUTES are different things. See
    CreditPathObserved. The counts are read off the adapter after the call, so they describe
    what happened rather than what was asked for.
    """
    if not address:
        print("\nADDRESS  (none given -- pass --address or set SOL_HOT_WALLET to check one)", flush=True)
        return CreditPathObserved(address_read=False, is_spl=adapter.is_spl,
                                  signatures=0, credits=0, refused=0)
    print(f"\nADDRESS  {address}", flush=True)
    print(f"    {describe_address(address)}", flush=True)
    if adapter.is_spl:
        run("associated token account", "derived locally; where an SPL balance for this owner actually lives",
            lambda: _ata_line(adapter, address))
    run("balance", "the token account's balance for an SPL mint, or lamports for native SOL",
        lambda: _balance_line(adapter, address))
    # COUNTED FROM THE ADAPTER, not from the rendered line. Parsing the sentence this step
    # prints would make the summary agree with the wording rather than with the run.
    credited: list[int] = []
    run(f"find_deposits_to_address(limit={limit})", "THE REAL METHOD the deposit watcher calls. '(none)' is a result.",
        lambda: _deposits_line(adapter, address, limit, credited))
    return CreditPathObserved(
        address_read=True,
        is_spl=adapter.is_spl,
        # `signatures_read` is a PROPERTY now -- listed minus unreadable. It was an attribute
        # holding the LISTED count, which is what made this report overstate its own coverage.
        signatures=adapter.signatures_read,
        credits=credited[0] if credited else 0,
        refused=sum(d.credits for d in adapter.unattributable_drops),
        unreadable=len(adapter.unreadable_signatures),
    )


def owner_of(adapter: SolanaAdapter, token_account: str) -> tuple[str, bool, str]:
    """The ACCOUNT that owns one token account. Returns (owner, throttled, why). Read-only.

    "ACCOUNT" RATHER THAN "WALLET": the owner may be a Program Derived Address, which holds
    tokens perfectly well and has no private key. Calling it a wallet told a reader a key
    exists -- see find_a_holder() for the run where that contradicted the next line of output.

    EXTRACTED FROM find_a_holder() because inlining it put that function at PLR0911 7 returns
    against a ceiling of 6, and CLAUDE.md rule 12 is explicit about which way that resolves:
    extract the decision, do not raise the ceiling. It is a decision in its own right -- "is
    this entry usable, and if not, is that the endpoint's fault or ours" -- and find_a_holder()
    is then a loop over entries.

    AN EMPTY OWNER WITH throttled=False IS "this entry is not usable", which the caller walks
    past: one malformed row must not hide every good row behind it. A THROTTLE is returned as a
    throttle so the caller can stop, because retrying the rest of the list against an endpoint
    that has started refusing just spends the budget.

    `data.parsed.info.owner` IS FROM DOCUMENTATION AND NOT MEASURED. No Solana cluster is
    reachable from the container this was written in (re-checked 2026-09-30: 403 through the
    proxy). A wrong path here yields an empty owner and the caller says it could not read one,
    naming the path -- it cannot credit anything or claim coverage it does not have.
    """
    info, throttled, why = call_with_backoff(
        adapter, "getAccountInfo", token_account,
        {"encoding": "jsonParsed", "commitment": BALANCE_COMMITMENT})
    if throttled:
        return "", True, why
    if info is None:
        return "", False, f"getAccountInfo failed: {why}"
    parsed = ((info.get("value") or {}).get("data") or {}).get("parsed") or {}
    owner = (parsed.get("info") or {}).get("owner") or ""
    if not owner:
        return "", False, "no `owner` under data.parsed.info"
    if not is_valid_address(owner):
        return "", False, f"`owner` is {owner!r}, which is not a valid Solana address"
    return owner, False, ""


class RevealingTx(NamedTuple):
    """The transaction that revealed an owner, and whether it CREDITS that owner.

    THE DISTINCTION IS THE WHOLE POINT, and it was missing on 2026-10-01. The map used to hold
    a bare signature and prove_the_spl_reader() called it "the ONE transaction known to carry a
    balance for this owner" -- which it was not. owner_in_post_token_balances() selects on the
    PRESENCE of a postTokenBalances entry for the mint and owner; `_spl_credits` requires a
    POSITIVE DELTA. An entry exists when the owner sent tokens, or when pre equals post, and in
    both cases the targeted proof reads a transaction that cannot possibly decode an amount.

    That is exactly what the operator's run did: the proof step printed

        (none)  <- no POSITIVE delta for this owner and mint in this transaction

    aimed at qGqMZv7Ljqpp7VaZ..., chosen only for having an entry. The line was true and the
    step was spent on a transaction selected for the wrong property.

    A NamedTuple because CreditPathObserved in this file already is one; a second container
    idiom in one module is rule 8's drift at the smallest possible scale.
    """

    signature: str
    #: True when post - pre > 0 for this owner and mint -- the condition _spl_credits requires.
    #: False means an entry exists and the delta does not qualify, so the proof step will come
    #: back "(none)" and says up front that it may.
    credits_owner: bool


#: owner -> the RevealingTx, filled in by holder_from_mint_traffic().
#:
#: A MODULE-LEVEL MAP RATHER THAN A WIDENED RETURN TUPLE, because find_a_holder() and
#: after_the_first_route_was_throttled() both return (owner, why) and three call sites would
#: have to grow a field they do not use. The alternative considered and rejected: parsing the
#: signature back out of the `why` sentence, which is reading prose as data -- the thing this
#: file has already been bitten by twice.
_revealed_by: dict[str, RevealingTx] = {}


def prove_the_spl_reader(adapter: SolanaAdapter, owner: str, run,
                         observed: CreditPathObserved) -> CreditPathObserved:
    """Run _spl_credits over the one transaction known to credit this owner, if it is still owed.

    WHY A TARGETED READ AND NOT A WIDER SCAN. Four runs against public devnet failed to prove
    this reader, and the 2026-09-30 `--limit 5` output showed why rather than leaving it to
    guesswork:

        GcBBd25S...   the holder --find-holder found in the mint's traffic
        GzprPkmd...   its ASSOCIATED token account -- DOES NOT EXIST
        0.0           so its WSOL lives in some OTHER token account
        5 of 5 fetched, filter matched nothing

    The scan reads the OWNER's recent signatures. The transaction that revealed the owner came
    from the MINT's history, so it need not be in that window at all -- and widening the window
    makes coverage worse on a rate-limited endpoint (measured: 8 of 50 against 9 of 10). No
    amount of scanning reliably reaches the one transaction already known to contain the entry.

    So read that transaction. `_credits_in_transaction` is the same per-transaction reader
    find_deposits_to_address calls, and reaching an amount through it exercises
    `entry["uiTokenAmount"]["decimals"]` and `["amount"]` -- the field names that lose an SPL
    deposit silently.

    AND ON 2026-10-01 THE SCAN GOT THERE FIRST, which is why this now takes `observed` and can
    decline to run. That run decoded one amount over 5 signatures and refused it for carrying no
    memo -- a refusal is a proof, since the decode happens before the memo check -- and this step
    went ahead anyway, spending a sixth getTransaction and printing "(none)" two lines above a
    SUMMARY correctly saying the reader was proven. Rule 3: prefer removing work to doing it
    faster. The step is owed only when the scan came back with nothing decoded.

    IT MAY STILL NOT DECODE WHEN IT DOES RUN, and that is now pre-announced rather than
    explained afterwards. `_spl_credits` needs a POSITIVE delta; holder_from_mint_traffic()
    prefers a transaction that has one and falls back to an entry-only owner when the window
    holds none, so the caption says which kind it is aimed at before the result appears --
    see _what_the_target_is() and RevealingTx.
    """
    reveal = _revealed_by.get(owner)
    if reveal is None:
        return observed
    if observed.decoded_an_amount:
        # ALREADY PROVEN, SO NOTHING IS SPENT PROVING IT AGAIN. The operator's 2026-10-01 run is
        # the case: find_deposits_to_address read 5 signatures, decoded one amount and refused it
        # for carrying no memo -- which IS the proof, since the decode happens before the memo
        # check -- and this step then spent a sixth getTransaction to add nothing. Worse, it
        # printed "(none)" two lines above a SUMMARY correctly saying the reader was proven, and
        # the nearer line is the one a reader believes. Rule 3: prefer removing work to doing it
        # faster.
        print(f"  _spl_credits: already PROVEN by find_deposits_to_address above "
              f"({observed.credits} credited, {observed.refused} refused), so the targeted read "
              f"over {reveal.signature[:16]}... is SKIPPED -- it would spend a getTransaction to "
              f"re-establish what the scan established.", flush=True)
        return observed
    # THE OUTCOME COMES BACK, and the OBSERVATION is what comes back rather than a flag -- same
    # reason CreditPathObserved exists at all. A list because `run` returns nothing (it prints
    # through its own `done`), which is the pattern _deposits_line's `credited` already uses.
    decoded: list[bool] = []
    run(f"_spl_credits over {reveal.signature[:16]}...", _what_the_target_is(reveal),
        lambda: _spl_reader_line(adapter, owner, reveal.signature, decoded))
    # NOT `any(decoded) or True`: an empty list means the step RAISED before recording, and a
    # step that died proved nothing. `any` over an empty list is False, which is correct here.
    return observed._replace(targeted_read_decoded=any(decoded))


def _what_the_target_is(reveal: RevealingTx) -> str:
    """The step's own caption, which has to say what the transaction was chosen FOR.

    "the ONE transaction known to carry a balance for this owner" is what this said, and it
    overstated the selection in exactly the way RevealingTx documents: the transaction was
    chosen for having a postTokenBalances ENTRY, which is not the positive delta _spl_credits
    needs. An operator reading a caption that promises a balance and a result of "(none)"
    concludes the reader is broken.
    """
    if reveal.credits_owner:
        return ("the one transaction known to CREDIT this owner (post - pre > 0) -- the "
                "targeted proof")
    return ("the only transaction holding an entry for this owner, and it does NOT credit it "
            "-- so '(none)' here is EXPECTED and proves nothing either way")


def _spl_reader_line(adapter: SolanaAdapter, owner: str, signature: str,
                     decoded: list[bool] | None = None) -> str:
    """What the targeted read established about _spl_credits' decoder.

    A STEP LINE DESCRIBES THE TRANSACTION IT READ, AND NOTHING WIDER. Two of these outcomes
    used to end with "the last reader in this adapter with no live evidence" -- a superlative
    about every reader across every run, asserted by a step that read one transaction. It also
    contradicted the sentence in front of it: your run printed "reads are PROVEN. That is the
    last reader ... with no live evidence", which denies what it had just established.

    That is the fourth time in this file a line has claimed something its own scope could not
    see (the summary's "has not decoded one", the summary built from a pre-step observation,
    the caption promising a balance the selection never checked for). The structural answer is
    the same each time and is worth stating once: cross-cutting conclusions belong in the
    SUMMARY, where CreditPathObserved is the authority on what ran. A step reports its own read.

    THREE OUTCOMES, AND THE FIRST VERSION OF THIS COLLAPSED TWO INTO A FALSE SENTENCE. It said
    "the entry exists but its delta is not POSITIVE" whenever the credit list came back empty --
    and an empty list ALSO happens when the amount was decoded fine and `_attributable` then
    dropped it for carrying no memo. Driven against a seeded response crediting 2.5 tokens, it
    printed "delta is not POSITIVE" directly under the adapter's own WARNING saying "1 credit(s)
    dropped". The decode had happened; the message denied it.

    THE DROPS EXIST PRECISELY TO TELL THOSE APART, and were added two days ago for exactly this
    distinction -- which makes this the fifth time one of this session's lessons had to be
    applied a second time. `unattributable_drops` is cleared and then read here, so a refusal
    counts as PROOF OF THE DECODER rather than as silence: every field name in the reader had to
    be right to produce an amount for the memo check to then decline.

    IT CALLS A PRIVATE METHOD ON PURPOSE. `_credits_in_transaction` is the same per-transaction
    reader find_deposits_to_address uses, and aiming at one signature is the whole point -- a
    scan cannot be. Making it public would widen the five-method contract for a diagnostic's
    sake. (No `noqa` here: SLF001 is not in this repo's ruff selection, and ruff's RUF100
    removed the one I first wrote. The reason stays even though the linter does not ask.)
    """
    # CLEARED FIRST: only find_deposits_to_address() clears this, and we are calling the
    # per-transaction reader directly, so whatever an earlier step recorded would be read as
    # belonging to this transaction.
    adapter.unattributable_drops = []
    # CAPTURED, like find_deposits_to_address's call is. Without this the adapter's WARNING for
    # a dropped credit reaches the root handler mid-step and prints unindented at column 0 --
    # see _capturing_adapter_logs() for the operator's run that showed it.
    with _capturing_adapter_logs() as captured:
        credits = adapter._credits_in_transaction(signature, owner, FINALIZED_RANK)
    dropped = sum(drop.credits for drop in adapter.unattributable_drops)
    # NOT `already_listed`: the step's label truncates the signature to 16 characters, so the
    # full one appears nowhere above this and the warning's copy is the only one.
    logged = _indented(captured.records)
    if decoded is not None:
        # DECODED, WHICH INCLUDES REFUSED. The amount is read before the memo check, so a
        # refusal means every field name in the reader was right -- the same rule
        # decoded_an_amount already applies to the scan's counts.
        decoded.append(bool(credits or dropped))

    if credits:
        amounts = ", ".join(f"{credit['amount']} (vout={credit['vout']})" for credit in credits)
        return (f"DECODED and ATTRIBUTED {len(credits)} credit(s): {amounts}  <- _spl_credits "
                f"read uiTokenAmount.decimals and .amount off a REAL response, and the memo "
                f"carried a usable tag."
                + logged)
    if dropped:
        return (f"DECODED {dropped} credit(s) and then REFUSED them: no usable memo, so nothing "
                f"is credited -- which is correct. But the amount WAS decoded off a real "
                f"response, so _spl_credits' uiTokenAmount.decimals and .amount reads are "
                f"PROVEN. Which is what this step exists to establish."
                + logged)
    return ("(none)  <- no POSITIVE delta for this owner and mint in this transaction, so "
            "_spl_credits returned before decoding an amount. A fact about this transaction, "
            "not about the field names -- and NOT the same as the reader never running."
            + logged)


def owner_in_post_token_balances(transaction: object, mint: str) -> str:
    """The first wallet in this transaction holding `mint`, read the way _spl_credits reads it.

    PURE, AND EXTRACTED because inlining it put holder_from_mint_traffic() at C901 11 against a
    ceiling of 10 -- rule 12: extract the decision, do not raise the ceiling. It is the decision
    worth having alone anyway: it is the SELECTION _spl_credits performs, with seeded inputs
    instead of a cluster, so the key path can be tested without a network.

    THE SAME TWO KEYS, DELIBERATELY. `meta.postTokenBalances[].mint` and `[].owner` are what
    chains/solana._spl_credits selects on (solana.py's `indexed()`), and the point of finding a
    holder this way is that getting an answer at all measures that path. Spelling them
    differently here would make the measurement meaningless -- so if that selection ever moves,
    this is a second site to change, and it says so (rule 8).
    """
    if not isinstance(transaction, dict):
        return ""
    balances = ((transaction.get("meta") or {}).get("postTokenBalances")) or []
    for balance in balances:
        if not isinstance(balance, dict) or balance.get("mint") != mint:
            continue
        owner = balance.get("owner") or ""
        if owner and is_valid_address(owner):
            return owner
    return ""


def credits_the_owner(transaction: object, mint: str, owner: str) -> bool:
    """Would _spl_credits find a POSITIVE delta for this owner and mint in this transaction?

    THE SECOND HALF OF THE SELECTION, and the half owner_in_post_token_balances() does not do.
    That function answers "is there an entry"; this answers "does the entry qualify", which is
    the question the targeted proof actually depends on -- see RevealingTx.

    SPELLED THE WAY _spl_credits SPELLS IT (chains/solana.py's `indexed()` plus its delta), so
    that a transaction this accepts is one that reader can decode: keyed by accountIndex,
    matched on owner AND mint, pre defaulting to zero when the account did not exist before,
    and `delta > 0` rather than `>=`. If that reader's arithmetic ever moves this is a second
    site to change, and so is owner_in_post_token_balances() twenty lines up (rule 8).

    IT IS A PREFERENCE, NOT A GATE. holder_from_mint_traffic() still returns an entry-only
    owner when no crediting transaction is in the window -- an owner is an owner, and the
    balance and ATA steps want it either way. What this changes is WHICH signature the proof
    step is aimed at, and whether the step says up front that it may come back empty.
    """
    if not isinstance(transaction, dict):
        return False
    meta = transaction.get("meta") or {}

    def indexed(entries):
        return {
            int(entry["accountIndex"]): entry
            for entry in entries or []
            if isinstance(entry, dict) and entry.get("owner") == owner and entry.get("mint") == mint
        }

    try:
        pre, post = indexed(meta.get("preTokenBalances")), indexed(meta.get("postTokenBalances"))
        for index, entry in post.items():
            before = int(pre[index]["uiTokenAmount"]["amount"]) if index in pre else 0
            if int(entry["uiTokenAmount"]["amount"]) - before > 0:
                return True
    except (KeyError, TypeError, ValueError):
        # A MALFORMED ENTRY IS "NOT A CREDIT", not a crash, and the caller cannot mistake the
        # two because this only ever PREFERS one signature over another -- the owner is returned
        # either way and the proof step reports its own outcome. Narrow by exception type rather
        # than `except Exception`: these three are what a wrong shape raises here, and anything
        # else is a defect in this function that should surface (rule 12, BLE001).
        return False
    return False


#: How many of the mint's own recent transactions to read looking for a holder. Small, because
#: each one is a getTransaction and the public endpoint is already rate-limiting this run.
HOLDER_SEARCH_TRANSACTIONS = 8


def holder_from_mint_traffic(adapter: SolanaAdapter, mint: str) -> tuple[str, bool, str]:
    """A holder of `mint`, found in the mint's own recent transactions. Returns (owner, throttled, why).

    WHY A SECOND STRATEGY EXISTS. getTokenLargestAccounts is the precise way to ask this, and on
    2026-09-30 / 10-01 the public devnet endpoint refused it twice in a row with HTTP 429 after
    three attempts each -- measured, from the operator's own runs, not inferred. A flag that
    cannot get past a rate limit is a flag that does not work, and the fallback below uses
    ONLY methods this cluster has already answered today:

        getSignaturesForAddress   proven -- it is how --hunt-memo settled both program ids
        getTransaction            proven -- the same hunt read ten of them

    AND IT MEASURES THE FIELD PATH THAT IS ACTUALLY UNPROVEN, which the largest-accounts route
    does not. `_spl_credits` selects on `meta.postTokenBalances[].owner` and `.mint`; those are
    exactly the keys read here. So an owner found this way is not just an address to check --
    getting it at all is evidence that the reader's own selection path is right, which is the
    thing --find-holder exists to make provable.

    THE OWNER IS THE OWNING ACCOUNT, which is the point: postTokenBalances reports the token
    account's OWNER alongside its mint, so no second lookup is needed and a token account cannot
    be mistaken for its owner (the mistake owner_of() exists to avoid on the other route). That
    owner may be a Program Derived Address -- ordinary, and not a wallet.

    STILL UNMEASURED IN ONE RESPECT, and said rather than implied (rule 17): whether the WSOL
    mint's own address has recent signatures at all. Transfers reference token accounts, and only
    some operations put the mint in a transaction's account keys. If it has none this returns
    empty and says so -- which is a fact about the mint, not about the field names.
    """
    signatures, throttled, why = call_with_backoff(
        adapter, "getSignaturesForAddress", mint,
        {"limit": HOLDER_SEARCH_TRANSACTIONS, "commitment": DISCOVERY_COMMITMENT})
    if throttled:
        return "", True, f"getSignaturesForAddress({mint[:8]}...) was throttled: {why}"
    if signatures is None:
        return "", False, f"getSignaturesForAddress failed: {why}"
    entries = list(signatures or [])
    if not entries:
        return "", False, (f"the mint account has no recent signatures of its own. Transfers "
                           f"reference token accounts rather than the mint, so this is a fact "
                           f"about {mint[:8]}..., not about any field name.")

    crediting, entry_only, throttled_why = scan_the_mints_transactions(adapter, mint, entries)
    if throttled_why:
        return "", True, throttled_why
    found = crediting or entry_only
    if found is None:
        return "", False, (f"read {len(entries)} of the mint's transactions and none carried a "
                           f"postTokenBalances entry for it with a readable `owner`. That is a "
                           f"finding about the response shape: those are the keys _spl_credits "
                           f"uses.")
    owner, reveal = found
    # THE ONE WRITER of _revealed_by, and it stays the one writer: the scan returns candidates
    # and this decides which becomes the proof step's target. Putting the write inside the scan
    # would mean a throttled walk could leave a half-filled map behind it.
    _revealed_by[owner] = reveal
    return owner, False, holder_found_sentence(reveal, read=len(entries))


def scan_the_mints_transactions(
    adapter: SolanaAdapter, mint: str, entries: list,
) -> tuple[tuple[str, RevealingTx] | None, tuple[str, RevealingTx] | None, str]:
    """Walk the mint's transactions. Returns (crediting, entry_only, throttled_why).

    EXTRACTED 2026-10-01 because preferring a crediting transaction over an entry-only one put
    holder_from_mint_traffic() at C901 12 and PLR0911 7, against ceilings of 10 and 6. Rule 12:
    extract the decision, do not raise the ceiling -- and the split falls along a real seam, the
    NETWORK WALK here against the PROSE in holder_found_sentence(), which is pure and testable
    with no cluster at all.

    BOTH CANDIDATES COME BACK, which is the whole reason this returns a pair rather than one
    answer. A transaction carrying a postTokenBalances entry for the owner is not necessarily
    one that CREDITS it -- an entry exists when the owner sent tokens, or when pre equals post
    -- and only a crediting one can prove _spl_credits' decoder. The caller prefers the first
    and falls back to the second, because an owner is still an owner for the balance and ATA
    steps even when nothing in the window credits it.

    `crediting` short-circuits the walk; `entry_only` holds the FIRST such owner seen, so a
    throttle partway through does not change which fallback a re-run settles on.
    """
    entry_only: tuple[str, RevealingTx] | None = None
    for entry in entries:
        signature = entry.get("signature") if isinstance(entry, dict) else None
        if not signature:
            continue
        transaction, tx_throttled, tx_why = call_with_backoff(
            adapter, "getTransaction", signature,
            {"encoding": "jsonParsed", "commitment": DISCOVERY_COMMITMENT,
             "maxSupportedTransactionVersion": MEMO_HUNT_TRANSACTION_VERSION})
        if tx_throttled:
            # A THROTTLE ENDS THE WALK AND IS NOT A FINDING (rule 17), and it discards the
            # entry-only candidate with it: reporting a fallback owner found before a rate
            # limit, without saying the walk was cut short, would read as "the window holds no
            # crediting transaction" when most of the window was never read.
            return None, None, f"reading {signature[:16]}... was throttled: {tx_why}"
        if transaction is None:
            # ONE UNREADABLE TRANSACTION MUST NOT END THE SEARCH, the same reason owner_of()'s
            # caller walks past an unusable entry: a single odd row would hide every good one.
            continue
        owner = owner_in_post_token_balances(transaction, mint)
        if not owner:
            continue
        reveal = RevealingTx(signature, credits_the_owner(transaction, mint, owner))
        if reveal.credits_owner:
            return (owner, reveal), entry_only, ""
        if entry_only is None:
            entry_only = (owner, reveal)
    return None, entry_only, ""


def holder_found_sentence(reveal: RevealingTx, *, read: int) -> str:
    """What finding this owner this way established. PURE -- no cluster, no map.

    TWO SENTENCES AND THEY MUST NOT BE ONE. Before 2026-10-01 there was a single sentence
    saying the transaction "has a postTokenBalances entry", and prove_the_spl_reader() went on
    to call it "the ONE transaction known to carry a balance for this owner". The operator's run
    shows what that cost: the proof step read a transaction selected for having an entry, found
    no positive delta, and printed "(none)" next to a SUMMARY correctly saying the reader was
    proven -- by the scan, not by the step. A reader takes the nearer, more specific line as the
    verdict, so the sentence has to say which kind of transaction this is BEFORE the step runs.
    """
    if reveal.credits_owner:
        return (f"FOUND in the mint's own traffic: transaction {reveal.signature[:16]}... "
                f"CREDITS this mint to that account (post - pre > 0, the condition "
                f"_spl_credits requires). Which also MEASURES "
                f"`meta.postTokenBalances[].owner` and `.mint` -- the exact keys _spl_credits "
                f"selects on, and the ones that were unproven.")
    return (f"FOUND in the mint's own traffic: transaction {reveal.signature[:16]}... has a "
            f"postTokenBalances entry for this mint owned by that account -- but NONE of the "
            f"{read} transaction(s) read CREDITS it (no post - pre > 0). The targeted proof "
            f"below will say so rather than read as though the reader failed. Finding the owner "
            f"at all still MEASURES `meta.postTokenBalances[].owner` and `.mint` -- the exact "
            f"keys _spl_credits selects on.")


def after_the_first_route_was_throttled(adapter: SolanaAdapter, mint: str,
                                        why_not: str) -> tuple[str, str]:
    """Try the mint's own traffic, and say which route answered. Returns (owner, why).

    EXTRACTED because folding three outcomes into find_a_holder() put it at PLR0911 8 returns
    against a ceiling of 6 (rule 12: extract the decision). The three are not interchangeable
    and that is the whole reason they are spelled out: BOTH throttled says re-run; the fallback
    answering and finding nothing is a finding about the response; the fallback answering with a
    holder means the run can continue, and it names which route produced the address so a reader
    is never left guessing which of the two was exercised.
    """
    found, second_throttled, second_why = holder_from_mint_traffic(adapter, mint)
    if found:
        return found, (f"getTokenLargestAccounts was throttled ({why_not}), so this came from "
                       f"the mint's own traffic instead. {second_why}")
    if second_throttled:
        return "", (f"BOTH routes were throttled. getTokenLargestAccounts: {why_not}. The mint's "
                    f"own traffic: {second_why}. The endpoint never answered either way, so this "
                    f"says NOTHING about the field names here or about who holds {mint}. Re-run "
                    f"in a moment, or against an endpoint that is not rate-limited.")
    return "", (f"getTokenLargestAccounts was throttled ({why_not}) -- that part says nothing "
                f"about our code. The fallback through the mint's own traffic DID answer and "
                f"found nothing usable: {second_why}")


def find_a_holder(adapter: SolanaAdapter, mint: str) -> tuple[str, str]:
    """Ask the cluster for an ACCOUNT that holds `mint`. Read-only. Returns (owner, why).

    "ACCOUNT", NOT "WALLET", AND THE OPERATOR'S 2026-10-01 RUN IS WHY. This returned
    218gaMJkJUkzPrfax8vEYKtpm3aj9dHUvYcnRnvKbLLp, described in its own message as "a wallet" --
    and the ADDRESS section two lines later printed

        valid, OFF-CURVE  <- a Program Derived Address (an Associated Token Account is one).
        No private key exists for it

    Two lines of one paste contradicting each other. A PDA owning a token account is entirely
    ordinary -- programs hold tokens -- so the finding is the WORD, not the address. And it is
    not cosmetic: "wallet" tells a reader a key exists, which is the one thing that is false
    here, and this script's whole job is saying what is known.

    A PDA IS A PERFECTLY GOOD SUBJECT for what --find-holder is for: `_spl_credits` matches on
    `postTokenBalances[].owner` and does not care whether that owner can sign. So nothing is
    filtered out -- only described correctly.

    WHY THIS EXISTS. `_spl_credits` was, when this was written on 2026-10-01, the last reader
    in this adapter with no real response behind it (it has one now, from the operator's run
    that first exercised this step), and it cannot be proven by pointing at an account that holds none of the token:
    it selects token balances by owner AND mint BEFORE touching an amount, so over an account
    with no token account it returns [] without ever reading
    `entry["uiTokenAmount"]["decimals"]` or `["amount"]`. Those are the field names that would
    lose an SPL deposit silently. The operator's 2026-09-30 run said exactly that -- "PARTLY
    exercised ... WITHOUT decoding an amount" -- and the only next step was for a human to go
    and find an address holding the token.

    That is a step the cluster can take instead. getTokenLargestAccounts names the biggest token
    ACCOUNTS for a mint; getAccountInfo on one of those, parsed, names the WALLET that owns it.
    Two reads, nothing signed, nothing sent, and the answer is somebody else's account -- which
    proves the reader exactly as well as our own would, on the same principle as --hunt-memo.

    THE FIELD NAMES BELOW ARE FROM DOCUMENTATION AND HAVE NOT BEEN MEASURED (rule 17). No
    Solana cluster is reachable from the container this was written in -- re-checked 2026-09-30,
    api.devnet.solana.com still answers 403 through the proxy. So this helper carries the same
    risk the rest of the file was written with, with one difference that makes it acceptable: a
    wrong field name here makes the HELPER fail and say so, and cannot credit anything or
    misreport what was proven. It refuses rather than guessing at every step.
    """
    largest, throttled, why_not = call_with_backoff(
        adapter, "getTokenLargestAccounts", mint, {"commitment": BALANCE_COMMITMENT})
    if throttled:
        # A RATE LIMIT IS NOT A FINDING ABOUT OUR FIELD NAMES (see call_with_backoff) AND IT IS
        # NOT THE END OF THE ATTEMPT EITHER. The operator's runs showed the public devnet
        # endpoint refusing getTokenLargestAccounts twice in a row, so falling back is the
        # difference between a flag that works and one that cannot get past a rate limit.
        return after_the_first_route_was_throttled(adapter, mint, why_not)
    if largest is None:
        return "", (f"getTokenLargestAccounts failed: {why_not}. The endpoint answered and the "
                    f"answer could not be used, which IS a finding -- the field names in "
                    f"find_a_holder() were written from documentation and never measured.")
    holders = (largest.get("value") or [])
    if not holders:
        return "", (f"getTokenLargestAccounts returned no holders for {mint}, so no account on "
                    f"this cluster holds it. Nothing to point at.")

    for holder in holders:
        token_account = holder.get("address")
        if not token_account:
            continue
        owner, throttled_here, owner_why = owner_of(adapter, token_account)
        if throttled_here:
            # REPORTED AS A THROTTLE EVEN MID-LOOP, rather than walked past as an unreadable
            # entry. Walking past would look identical to "this holder has no owner field",
            # which is the finding-versus-endpoint confusion one level down.
            return "", (f"the endpoint THROTTLED the owner lookup for {token_account} "
                        f"({owner_why}). The mint's holders were read; who owns them was not. "
                        f"Re-run in a moment.")
        if owner:
            return owner, (f"FOUND by asking the cluster: token account {token_account} holds "
                           f"{holder.get('uiAmountString', '?')} and is owned by this account. "
                           f"Two reads, nothing sent.")
    return "", (f"getTokenLargestAccounts named {len(holders)} token account(s) for {mint} and "
                f"none of them reported a readable `owner` under data.parsed.info -- which is a "
                f"finding about the response shape, not about the mint. The field names in "
                f"find_a_holder() were written from documentation and never measured.")


def resolve_address(explicit: str, hot_wallet: str, deposit_account: str = "") -> tuple[str, str]:
    """Which account to read, and WHY that one. Returns (address, why).

    FOUR SOURCES IN PRECEDENCE ORDER, and the second arrived 2026-10-01 after a run that read
    the wrong account without saying so:

        --address              what the operator typed. Always wins.
        SOL_DEPOSIT_ACCOUNT    where every customer deposit is told to go.
        SOL_HOT_WALLET         the configured payout wallet, if there is one.
        SOLANA_DEVNET_ACCOUNT  a known-funded devnet account this repository already holds.

    WHY THE DEPOSIT ACCOUNT OUTRANKS THE HOT WALLET, which is the whole point of the change.
    The ADDRESS section's headline step is find_deposits_to_address -- labeled in this file as
    "THE REAL METHOD the deposit watcher calls" -- and the watcher calls it on
    `swap["deposit_address"]`, which for a tag-attributed chain IS SOL_DEPOSIT_ACCOUNT
    (services/swap_service.deposit_account()). SOL_HOT_WALLET is the PAYOUT side: config.py is
    explicit that it is a public key for sending, and nothing deposits into it.

    So with SOL_DEPOSIT_ACCOUNT set and SOL_HOT_WALLET unset, this function returned the devnet
    fallback and the run exercised the deposit path against an account that receives no
    deposits -- while the summary four lines below printed `SOL_DEPOSIT_ACCOUNT CUBnQ5QB... <-
    SET`. Two true statements about two different accounts, and nothing reconciled them. That
    is the same shape as the balance-versus-delta confusion earlier the same day: the step and
    the summary each correct, the reader unable to tell they were about different things.

    WHAT THE THIRD FIXES. Until 2026-09-30 there was no third, so a run with no --address
    printed "(none given -- pass --address or set SOL_HOT_WALLET to check one)" and skipped the
    entire ADDRESS section -- which is the section that exercises getBalance, getAccountInfo and
    find_deposits_to_address, the credit path, the one place a wrong field name loses a deposit
    rather than raising. The check most worth running was the one that needed an argument, and I
    then handed the operator that argument as a placeholder inside a code block. They pasted it:

        python3 solana_chain_check.py --address <a devnet wallet with a balance>
        bash: syntax error near unexpected token `newline'

    A script that cannot run without a value its own repository already knows is a script that
    mostly does not get run. The address has been in fund_testnets.py since it was written; it
    lives in chains/solana_address.py now so both can reach it.

    THE REASON IS RETURNED, NOT JUST THE ADDRESS. A default that appears silently is a default
    an operator reads as their own configuration -- and the difference matters here, because
    getBalance against the fallback says the READ PATH works and says nothing about whether
    SOL_HOT_WALLET is set correctly. Rule 14: echo the parameter that decides the answer.
    """
    if explicit:
        return explicit, "from --address"
    if deposit_account:
        return deposit_account, (
            "from SOL_DEPOSIT_ACCOUNT -- the account every SOL deposit is told to go to, and "
            "the one find_deposits_to_address runs against in production. THIS is the deposit "
            "path"
        )
    if hot_wallet:
        return hot_wallet, (
            "from SOL_HOT_WALLET, which is the PAYOUT wallet -- no deposit is told to go there, "
            "so this proves the read path against a real account and not the deposit path. Set "
            "SOL_DEPOSIT_ACCOUNT to check that one"
        )
    return SOLANA_DEVNET_ACCOUNT, (
        "DEFAULTED to the devnet account this repo already knows (chains/solana_address."
        "SOLANA_DEVNET_ACCOUNT) -- no --address, no SOL_DEPOSIT_ACCOUNT and no SOL_HOT_WALLET. "
        "This proves the READ PATH and says nothing about your own wallet being configured"
    )


def memo_status_lines(hunted: bool | None) -> list[str]:
    """What the summary should say about the memo program ids, DERIVED rather than written.

    `hunted` is None when no hunt ran this time, True when one ran and confirmed at least one
    id, False when one ran and confirmed none. The measured/unmeasured split itself comes from
    chains/solana_memo.MEASURED_MEMO_PROGRAM_IDS, which is the only place that knows.

    THIS FUNCTION EXISTS BECAUSE THE SUMMARY TOLD THE OPERATOR THE OPPOSITE OF WHAT THE SAME RUN
    HAD JUST PRINTED. On 2026-09-30 a `--hunt-memo 50` run printed two CONFIRMED lines, one per
    program id, and then four lines later:

        What is still unproven is the memo PROGRAM IDS, written from documentation and never
        seen on a cluster: re-run with --hunt-memo N to settle them against real transactions.

    Telling somebody to re-run the thing they had just run, to settle what it had just settled.
    That is the SIXTH copy of this defect in one day and it is the one with the least excuse:
    `hunt_memo()` returns whether an id was confirmed, its docstring says it returns that "so a
    caller can print the difference", and main() threw the value away -- in the same commit
    where MEASURED_MEMO_PROGRAM_IDS was added specifically so this banner could not drift. I
    derived the per-id line at the TOP of the hunt and hand-wrote the one at the bottom.
    """
    measured = [p for p in MEMO_PROGRAM_IDS if p in MEASURED_MEMO_PROGRAM_IDS]
    unmeasured = [p for p in MEMO_PROGRAM_IDS if p not in MEASURED_MEMO_PROGRAM_IDS]
    lines = [
        f"  memo program ids: {len(measured)} of {len(MEMO_PROGRAM_IDS)} measured against a real "
        f"cluster{', ' + str(len(unmeasured)) + ' not' if unmeasured else ''}.",
    ]
    lines.extend(
        f"    {program}  <- NOT YET MEASURED; --hunt-memo N settles it" for program in unmeasured
    )
    if hunted is False:
        lines.append("    this run's hunt confirmed NOTHING -- see its own lines above for why; a "
                     "throttled hunt is not a finding")
    if hunted is None and unmeasured:
        lines.append("    no hunt ran this time. Add --hunt-memo N to settle the id(s) above.")
    return lines


class CreditPathObserved(NamedTuple):
    """What the run ACTUALLY did on the deposit path. Observations, not flags.

    WHY THIS REPLACED TWO BOOLEANS, and it is the fourth attempt at this sentence rather than
    the first. Coverage was reported from which FLAGS were passed -- `read_address` and
    `read_mint` -- and inferring what executed from what was requested produced a false claim
    every time the shape of the run changed:

      2026-09-30  "have never met a real response, because this run passed no --address" printed
                  unconditionally, so a run that DID pass one was told its coverage never
                  happened.
      2026-09-30  the fix for that named getAccountInfo on the native line. getAccountInfo is
                  SPL-only and is never called without a mint.
      2026-09-30  the fix for THAT said "both credit readers" for a --mint run in which
                  `_spl_credits` matched nothing. Its filter ran over eight transactions and
                  short-circuited; `entry["uiTokenAmount"]["decimals"]` and `["amount"]` -- the
                  field names that would silently lose an SPL deposit -- were never read.

    Three corrections to one inference is the inference being wrong (rule 19: fix the cause).
    The counts below come off the adapter after the call, so the report describes the run.

    THE DISTINCTION THAT MATTERS AND THAT FLAGS CANNOT SEE: a reader whose filter matched
    nothing is not a reader that has been exercised. `_spl_credits` selects token balances by
    owner AND mint before touching an amount, so over an account with no token account it
    returns [] without ever decoding one. "It ran" and "it decoded a real amount" are different
    claims, and only the second retires the risk this whole script exists for.
    """

    address_read: bool
    is_spl: bool
    signatures: int
    credits: int
    refused: int
    #: Signatures the scan LISTED and could not fetch. A credit may be in any of them, so a
    #: report saying the reader "matched nothing" over `signatures` overstates itself by this
    #: many. Added 2026-10-01 after the operator's run: one getTransaction answered HTTP 429,
    #: the adapter skipped it and said so in its log, and this summary still claimed the filter
    #: had run over all ten.
    unreadable: int = 0
    #: Did the TARGETED read (prove_the_spl_reader) decode an amount? Added 2026-10-01, because
    #: the run that first exercised that step printed the step saying
    #:
    #:   DECODED 1 credit(s) and then REFUSED them ... reads are PROVEN
    #:
    #: and then a SUMMARY saying
    #:
    #:   the reader returned no credits WITHOUT decoding an amount -- its
    #:   uiTokenAmount/balance-delta reads are still unproven
    #:
    #: Both lines were computed honestly from what each could see: `observed` is filled in by
    #: check_address, which runs BEFORE the targeted step, so the headline conclusion of the run
    #: was built from an observation taken before the thing that settled it. That is the same
    #: defect this class was created to stop -- a conclusion reported from something other than
    #: what executed -- in the one direction the class did not cover.
    targeted_read_decoded: bool = False

    @property
    def reader(self) -> str:
        return "_spl_credits" if self.is_spl else "_native_credits"

    @property
    def decoded_an_amount(self) -> bool:
        """Did the reader get past its filter and decode a real amount from a real response?

        A REFUSED credit counts: the amount was decoded and THEN the memo check declined it, so
        every field name in the reader had to be right to get that far.
        """
        return bool(self.credits or self.refused or self.targeted_read_decoded)


#: What the summary block wraps to. The hand-wrapped literals in this file sit at 78-85
#: characters rendered, so this is the width they already use rather than a new choice.
SUMMARY_WIDTH = 84


def _wrapped(*sentences: str) -> list[str]:
    """Prose wrapped to the summary block's width, with its two-space indent. ONE PLACE.

    EVERY HAND-WRAPPED LITERAL IN THIS BLOCK HAS BEEN WRONG AT LEAST ONCE, and the instances
    were being fixed one at a time: f8c3b1f shipped a 101-character line against neighbours at
    81; 93b108b fixed that and left a 25-character line reading just "5 FETCHED signature(s),";
    the next version produced a 179-character sentence and a 367-character one, the second of
    which is the 42-unfetched case the operator actually hit. Rule 19's test -- does the fix
    stop the symptom being reported, or stop the cause existing -- says wrap once instead of
    measuring literals by hand forever.

    IT TAKES SENTENCES, NOT LINES. A caller that hands over pre-broken lines is hand-wrapping
    again; handing over whole sentences lets the width be the only thing that decides. The
    variable-length clauses are exactly where this matters, because their rendered length
    depends on counts nobody can see while writing the f-string.
    """
    return [f"  {line}" for line in
            textwrap.wrap(" ".join(s for s in sentences if s),
                          width=SUMMARY_WIDTH, break_long_words=False,
                          break_on_hyphens=False)]


def credit_path_lines(observed: CreditPathObserved) -> list[str]:
    """What the run proved about the deposit-credit path, which is the half that loses money."""
    if not observed.address_read:
        return _wrapped(
            "CREDIT path: NOT exercised, and it is the half that matters. getBalance,",
            "getAccountInfo and find_deposits_to_address did not run, because no address was",
            "read. A wrong field name there loses a deposit rather than raising.")

    # "NOT EXERCISED", NOT "HAS NOT DECODED ONE". The line below used to say the second, and it
    # is a claim about a reader this run never invoked -- CreditPathObserved holds nothing about
    # the other one and cannot, since a run is either SPL or native. Stated flatly it reads as a
    # measurement of that reader's state across every run, which is exactly rule 17's failure:
    # a reason to believe something written in the same voice as having checked it. What this
    # run establishes is which reader it exercised; what it owes the operator is how to exercise
    # the other. Caught 2026-10-01 reading the operator's fifth clean --find-holder run, where
    # the summary asserted _native_credits had decoded nothing in a run that never called it.
    other = "_spl_credits (needs --mint)" if not observed.is_spl else "_native_credits (drop --mint)"
    unexercised = (f"Every field name in that reader had to be right to get there. The other "
                   f"reader, {other}, was NOT exercised by this run -- a run reads one or the "
                   f"other, never both, so nothing here says whether it works.")
    if observed.decoded_an_amount:
        # WHICH READ GOT THERE, because the two are different evidence and the operator has to
        # be able to tell them apart. The scan proves the reader over a WINDOW; the targeted
        # read proves it over ONE transaction chosen for carrying a credit. Reporting the
        # second as "0 credited, 0 refused over 5 signature(s)" -- which the scan's counts are
        # when the targeted read is what decoded -- would say the opposite of what happened.
        if observed.targeted_read_decoded and not (observed.credits or observed.refused):
            how = (f"CREDIT path: {observed.reader} DECODED a real amount from the TARGETED "
                   f"read over the one transaction known to credit this owner. The "
                   f"{observed.signatures} signature(s) in the scan's own window decoded "
                   f"nothing, which is a fact about that window and not about the reader.")
        else:
            how = (f"CREDIT path: {observed.reader} DECODED a real amount from a real response "
                   f"({observed.credits} credited, {observed.refused} refused over "
                   f"{observed.signatures} signature(s)).")
        return _wrapped(how, unexercised)
    if observed.signatures:
        # THE DENOMINATOR IS WHAT WAS FETCHED, NOT WHAT WAS LISTED. "matched nothing over 10" is
        # a claim about ten transactions; if one was never read, a credit may be in it and the
        # claim is not established over the full set. The unread count is named, not folded in.
        # A BIGGER WINDOW MAKES COVERAGE WORSE ON A RATE-LIMITED ENDPOINT, and that is the
        # opposite of everyone's instinct including mine -- I suggested --limit 50 to the
        # operator. MEASURED on their two runs against public devnet:
        #
        #     --limit 10   9 of 10 fetched
        #     --limit 50   8 of 50 fetched
        #
        # Each listed signature costs a getTransaction, so raising the limit spends the rate
        # budget on listing instead of reading and FEWER transactions come back. Said on the
        # screen, because an operator looking at "42 never fetched" will reach for a bigger
        # number next (rule 14: the instruction has to be where the number is).
        advice = ""
        if observed.unreadable > observed.signatures:
            advice = (f"A SMALLER --limit will cover MORE here, not less: every listed signature "
                      f"costs a fetch, so a bigger window spends the rate budget on listing. "
                      f"{observed.unreadable} unfetched against {observed.signatures} fetched "
                      f"means the endpoint is the limit, not the window.")
        skipped = ("" if not observed.unreadable else
                   f"Plus {observed.unreadable} LISTED but never fetched, so a credit may be in "
                   f"those and this is NOT established over the full set.")
        # WHOLE SENTENCES, EACH ONE SELF-CONTAINED, because an optional clause in the middle
        # orphans a continuation. "and matched nothing" used to be a separate piece attached to
        # the count, and with `advice` between them the wrapped block read "...not the window.
        # and matched nothing" -- a lowercase continuation after a full stop. The old
        # `skipped or ','` existed for the same reason, to glue a fragment onto a count, and it
        # goes with it: the count's sentence now ends itself.
        return _wrapped(
            f"CREDIT path: PARTLY exercised. Discovery ran, and the filter in "
            f"{observed.reader} ran over {observed.signatures} FETCHED signature(s) and matched "
            f"nothing, so the reader returned no credits WITHOUT decoding an amount -- its "
            f"uiTokenAmount/balance-delta reads are still unproven, and those are the field "
            f"names that lose a deposit silently.",
            skipped,
            advice,
            "Point --address at an account that has received one.")
    return _wrapped(
        f"CREDIT path: discovery ran and returned ZERO signatures, so {observed.reader} was",
        "never invoked at all. Nothing about the readers was established. Point --address at",
        "an account with recent activity.")


def deposit_account_lines(account: str) -> list[str]:
    """Whether the shared account a SOL deposit needs is actually configured. PURE.

    THE SUMMARY SAID THE STRATEGY "IS DECIDED" AND NEVER SAID WHETHER IT WAS SET UP, and those
    read the same to somebody checking readiness. Measured on the operator's host 2026-10-01,
    which is what found this:

        adapters: ['GRC', 'SOL']        <- a SOL adapter IS constructed
        SOL_DEPOSIT_ACCOUNT: ''        <- and no SOL swap can be created

    So the chain's read path can pass every step in this check, against a real cluster, while
    the terminal cannot accept one SOL deposit -- and nothing in the output said so. This file
    mentioned SOL_DEPOSIT_ACCOUNT nowhere at all (grepped), though it is the variable that
    decides where a customer's coins are told to go.

    THE ACCOUNT IS PRINTED IN FULL, not truncated. It is a public address, and it decides where
    money is sent: an operator has to be able to read it off the paste and check it against the
    wallet they actually hold (rule 14, echo the parameters that decide the answer). Nothing
    secret is in it -- config.py's comment is explicit that the SOL hot wallet and this account
    are public keys and that nothing in chains/solana.py reads a keypair.
    """
    if account.strip():
        return [f"  SOL_DEPOSIT_ACCOUNT  {account.strip()}",
                "  <- SET, so a SOL swap has somewhere to send deposits. CHECK IT against the "
                "wallet you hold:",
                "     every SOL deposit for every swap is told to go here."]
    return ["  SOL_DEPOSIT_ACCOUNT  (unset)",
            "  <- so services/swap_service.py REFUSES to create a SOL swap, whatever this "
            "check proved.",
            "     The strategy above is decided in CODE; the account it needs is "
            "unconfigured, and there is",
            "     no default by design -- an account that decides where money lands is not "
            "something to infer."]


def print_summary(failures: list[str], elapsed: float, hunted: bool | None = None,
                  observed: CreditPathObserved | None = None) -> int:
    """Rule 14: a run that found nothing and a run that failed must not share a line."""
    print("\nSUMMARY", flush=True)
    total = format_duration(elapsed)
    if failures:
        print(f"  FAILED  {len(failures)} step(s): {', '.join(failures)}", flush=True)
        print("  A failing step here is the adapter meeting a real cluster for the first time. The likely cause is a", flush=True)
        print("  response field name that differs from what chains/solana.py expects -- see its header on why that", flush=True)
        print("  could not be verified where it was written.", flush=True)
        print(f"  checked in    {total}", flush=True)
        return 1
    print("  PASSED  every step answered. The adapter's read path works against this cluster.", flush=True)
    print("  NOT PROVEN by this run: nothing was sent. get_new_address() and send_to_address() refuse by design.", flush=True)
    # THE LINE THIS REPLACES WAS STALE AND THE OPERATOR READ IT OFF THEIR OWN SCREEN.
    # It said "the deposit-address strategy is still the operator's choice (README.md, 'Solana
    # deposit addresses')" -- decided on 2026-09-29, which is eleven days before this run, and
    # it pointed at a README section that now says DECIDED. So the script sent the reader to a
    # document to be told the opposite of what the script had just told them.
    #
    # It survived the 2026-09-30 sweep that fixed the same sentence in admin_view, swap_view,
    # the regtest panel and README because I grepped for the phrases those four used -- "not
    # decided", "custody choice is the operator's", "until it is settled" -- and this one is
    # spelled "still the operator's choice". That is rule 2's warning exactly: grep the tree
    # for the NAME, and a phrase is not a name. The fifth copy was found by an operator
    # pasting output back, which is how every one of these gets found.
    print("  The deposit-address strategy IS decided: one shared account plus a per-swap Memo", flush=True)
    print("  instruction, chosen 2026-09-29. get_new_address() refuses BECAUSE of that choice --", flush=True)
    print("  under a shared account there is no per-swap address to derive.", flush=True)
    for line in deposit_account_lines(Config.SOL_DEPOSIT_ACCOUNT):
        print(line, flush=True)
    # DERIVED, and the two lines this replaces were hand-written and contradicted the same run's
    # own output four lines earlier. See memo_status_lines().
    for line in memo_status_lines(hunted):
        print(line, flush=True)
    # WHAT THIS RUN ACTUALLY COVERED, derived from whether an address was read rather than
    # asserted. These three lines used to say the credit path "have never met a real response,
    # because this run passed no --address and no --mint" -- unconditionally, so the first run
    # that DID pass one would have been told its own coverage did not happen. Same defect as the
    # memo lines two commits ago, in the paragraph written to replace them.
    for line in credit_path_lines(observed or CreditPathObserved(False, False, 0, 0, 0)):
        print(line, flush=True)
    print(f"  checked in    {total}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Solana adapter check. Broadcasts nothing, signs nothing, reads no key.",
    )
    parser.add_argument("--address", default="", help="a wallet address to inspect (defaults to SOL_HOT_WALLET)")
    parser.add_argument("--mint", default="", help="an SPL mint to inspect (defaults to SOL_SPL_MINT)")
    parser.add_argument("--limit", type=int, default=10, help="how many recent signatures to read for the address")
    parser.add_argument(
        "--find-holder", action="store_true",
        help="ask the cluster for a wallet that actually holds --mint and check THAT one. "
             "Read-only: getTokenLargestAccounts then getAccountInfo. This is what proves "
             "_spl_credits decodes a real amount, which an account holding none of the token "
             "cannot do.",
    )
    parser.add_argument(
        "--hunt-memo", type=int, default=0, metavar="N",
        help="also read N recent transactions from the Memo program itself and check that "
             "chains/solana_memo.py recognizes them (read-only; settles the program id "
             "without sending anything)",
    )
    args = parser.parse_args()

    # .copy() RATHER THAN dict(), 2026-10-09, and the difference is the type that
    # comes back. Config.RPC["SOL"] is a config.SolanaRpc now, and a TypedDict's
    # .copy() returns the same TypedDict while dict() returns a plain dict whose
    # values are `object` -- which is what made every read below unverifiable and
    # `SolanaAdapter(**rpc)` uncheckable against the six parameters it is built
    # for. Still a shallow copy of the same keys, and still copied for the same
    # reason: --mint overwrites one of them and must not edit the process's config.
    rpc = Config.RPC["SOL"].copy()
    if args.mint:
        rpc["mint"] = args.mint
    # SOL_DEPOSIT_ACCOUNT comes off Config DIRECTLY and not out of rpc[], because it is not an
    # RPC parameter -- it is custody configuration, and config.py keeps it as its own attribute
    # for that reason. Passed here so the ADDRESS section aims at the account the deposit
    # watcher actually scans; see resolve_address() for the run that read the wrong one.
    # rpc["hot_wallet"] AND NOT .get(...) or "": config.SolanaRpc declares it a
    # `str` and this dict is a copy of config.py's own entry, where
    # _env("SOL_HOT_WALLET", "") already answers "" for unset. The `or ""` was
    # getting an `object` past resolve_address()'s `str` parameter.
    address, address_why = resolve_address(
        args.address, rpc["hot_wallet"], Config.SOL_DEPOSIT_ACCOUNT)

    started = time.monotonic()
    print_banner(rpc, address, address_why)
    if not rpc["url"]:
        print("  FAIL  SOL_RPC_URL is unset, so there is nothing to check. Set it and run again.", flush=True)
        return 1

    adapter = SolanaAdapter(**rpc)
    # ASKED AFTER THE ADAPTER EXISTS AND BEFORE THE SECTIONS RUN, so the banner has already
    # printed the address it was going to use and this prints the override with its reason.
    # Rule 14: a value that decides the answer is echoed next to the answer, and a DIFFERENT
    # account than the banner named would otherwise be read silently.
    if args.find_holder:
        print(flush=True)
        if not rpc.get("mint"):
            print("  --find-holder needs --mint: it looks for a wallet holding a TOKEN, and "
                  "native SOL has no holders to look up.", flush=True)
            return 1
        print(f"  --find-holder: asking the cluster who holds {rpc['mint']} (read-only)", flush=True)
        try:
            found, why = find_a_holder(adapter, rpc["mint"])
        except Exception as exc:  # noqa: BLE001 -- checked: a diagnostic. find_a_holder() already returns its throttles and shape findings as reasons, so reaching HERE means something it did not anticipate; it is PRINTED and the run stops rather than falling back to an address that proves nothing.
            print(f"  that lookup RAISED, which find_a_holder() should have returned instead: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return 1
        # NO BLANKET ATTRIBUTION HERE ANY MORE. This printed "the field names in find_a_holder()
        # were written from documentation and never measured -- this is the finding, not a
        # crash" after EVERY failure, and the operator's first run failed with HTTP 429 -- a
        # rate limit, which says nothing about any field name. find_a_holder() distinguishes the
        # two now and its `why` carries the right one.
        print(f"  {why}", flush=True)
        if not found:
            return 1
        # ONLY `address` IS REASSIGNED. The first version of this also set `address_why`, which
        # nothing reads after print_banner() has run -- dead on arrival (rule 9), and ruff does
        # not flag a rebind of a name that WAS used earlier. The reason is printed on its own
        # line instead, which is what the banner's version of it was for.
        address = found
        print(f"  reading {address} instead of the address in the banner", flush=True)
    failures: list[str] = []
    run = make_runner(failures)
    check_cluster(adapter, run)
    check_rent(adapter, run)
    check_mint(adapter, run)
    observed = check_address(adapter, address, args.limit, run)
    # ONE GUARD, AND IT IS INSIDE THE FUNCTION. This read
    # `if args.find_holder and adapter.is_spl:`, and two mutation rounds on 2026-10-01 showed
    # BOTH halves were inert -- each spelled, in a second and third way, the question
    # prove_the_spl_reader() already asks as `_revealed_by.get(owner)`:
    #
    #   args.find_holder   `_revealed_by` is written at exactly one site
    #                      (holder_from_mint_traffic, line ~506), reached only from
    #                      find_a_holder(), called only from the --find-holder branch above.
    #   adapter.is_spl     find_a_holder() refuses without a mint, so a native run cannot put
    #                      anything in that map either.
    #
    # Deleting either changed no behavior, which is why no test could kill them. That is rule
    # 8's defect rather than a gap in coverage: three copies of one condition agree on the day
    # they are written, and the day a second caller fills that map the two outer copies start
    # refusing a proof the inner one would allow. The map is the authority;
    # test_no_proof_step_runs_when_no_signature_revealed_the_owner pins the refusal and
    # test_main_prints_no_proof_step_for_an_address_the_operator_supplied pins it through main().
    observed = prove_the_spl_reader(adapter, address, run, observed)
    # None means NO HUNT RAN, which memo_status_lines() renders differently from a hunt that
    # ran and confirmed nothing. Initialized here rather than only inside the branch: the first
    # version of this assigned it only under `if args.hunt_memo > 0`, so every plain run -- the
    # common case, and the one the operator runs most -- would have hit a NameError at the
    # summary. ruff does not flag a conditionally-bound local, and the tests that caught it are
    # the ones that call main() on both paths.
    hunted: bool | None = None
    if args.hunt_memo > 0:
        hunted = hunt_memo(adapter, args.hunt_memo)
    # THE RETURN VALUE IS USED NOW. hunt_memo()'s docstring has always said it returns whether
    # an id was confirmed "so a caller can print the difference", and this line discarded it --
    # which is how the summary came to tell the operator the ids were unproven immediately after
    # printing two CONFIRMED lines.
    return print_summary(failures, time.monotonic() - started, hunted, observed)


def what_the_hunt_established(program: str, *, seen: int, read: int, unread: int,
                              throttled: int = 0) -> tuple[bool, list[str]]:
    """What one program id's hunt actually proved, as (confirmed, lines to print).

    A FUNCTION RATHER THAN THREE BRANCHES INSIDE THE LOOP, for rule 10's reason and rule 12's:
    this is the DECISION the hunt exists to make, and inside the loop the only way to exercise
    it was to reach a cluster. Here it takes three integers and can be asserted on directly --
    which is also how the defect below is now pinned instead of described.

    THE THIRD OUTCOME IS THE ONE THAT WAS MISSING, and leaving it out printed a falsehood.
    Measured 2026-09-29 against api.devnet.solana.com: all 20 transactions for the second
    program id came back HTTP 429, so `read` was 0 and `seen` was 0 -- and with only two
    branches, zero-read fell into the "we looked and found nothing" arm. It printed

        read transactions for this id and found NO memo our parser recognizes.
        ... If it says jsonParsed, the id is wrong.

    Nothing had been read. The reading it invited -- that the id is wrong -- was unsupported,
    and a reader who acted on it would have changed a correct constant. That is rule 14's
    defect inside the very file whose job is telling two silences apart, and rule 17's
    register error in printed form: a hypothesis stated where a measurement belongs.

    So the three outcomes are distinct and named:

        seen > 0            CONFIRMED. Our parser read a real memo under this id.
        read == 0, throttled  NOT ESTABLISHED, and the reason is the ENDPOINT. Separated
                            from the line below on 2026-09-30 -- see the next paragraph.
        read == 0, no throttle  NOT ESTABLISHED, and the reason is that what came back could
                            not be used. Still not evidence about the id, but a different
                            thing to go and look at.
        read > 0, seen == 0 We read transactions and found no memo we recognize. THAT is
                            evidence, and the encoding line says which kind.

    THROTTLED IS ITS OWN COUNT BECAUSE THE OPERATOR'S RUN PROVED THE LINE WAS UNREADABLE.
    Until 2026-09-30 a throttle was counted as `unread`, so the zero-read branch said "all N
    transaction(s) were unreadable" and then, in the same breath, "HTTP 429 means the endpoint
    throttled us, not that the id is bad" -- telling the reader the count it had just given
    them was the wrong count. It also buried the actionable instruction (re-run smaller, or
    against a paid endpoint) at the end of five lines about something else.
    """
    if seen:
        return True, [
            f"    CONFIRMED: {program} is a real Memo program id and",
            "    chains/solana_memo.memo_strings_in() reads its instructions.",
        ]
    if read == 0 and throttled:
        return False, [
            f"    NOT ESTABLISHED -- THE ENDPOINT, NOT THE ID. {throttled} read(s) were refused",
            f"    with HTTP 429 and {unread} failed for another reason, so NOTHING was parsed",
            "    under this id. That is neither evidence it is right nor that it is wrong.",
            "    WHAT TO DO: re-run with a smaller N (--hunt-memo 5), or point SOL_RPC_URL at an",
            "    endpoint that is not rate-limited. A local solana-test-validator has no limit",
            "    but also no memo traffic, so a paid devnet endpoint is what settles this one.",
        ]
    if read == 0:
        return False, [
            f"    NOT ESTABLISHED: all {unread} transaction(s) failed to be read, and none was a",
            "    rate limit, so the reasons above are about what the endpoint SENT rather than",
            "    about whether it would answer. Nothing was parsed, so this says nothing about",
            "    the program id either way -- but the reasons are worth reading: an encoding or",
            "    field-shape failure here is a finding about chains/solana.py.",
        ]
    return False, [
        f"    read {read} transaction(s) for this id and found NO memo our parser recognizes.",
        "    If the encoding line above says NOT jsonParsed, that is the cause and the program",
        "    id is still unsettled. If it says jsonParsed, the id is wrong.",
    ]


#: How many throttles in a row mean the endpoint is not going to answer, so stop asking.
#:
#: MEASURED ON THE OPERATOR'S RUN, 2026-09-30, `--hunt-memo 50`: the first ten reads answered
#: and then forty in a row returned HTTP 429. Not one recovered. The hunt asked all forty
#: anyway, printed a six-line error block for each, and then started the second program id and
#: threw away twenty-two more the same way before the operator pressed Ctrl-C. Three is the
#: number because zero of the forty recovered: there is no evidence that a fourth try after
#: three consecutive refusals is worth the wait, and waiting is what made the run unbearable.
MEMO_HUNT_GIVE_UP_AFTER_THROTTLES = 3

#: How many times ONE call is attempted before it is reported as throttled. A DIFFERENT
#: DECISION from MEMO_HUNT_GIVE_UP_AFTER_THROTTLES, which counts REPORTED throttles in a row
#: across different signatures -- they shared the constant at first and that was rule 8's
#: two-copies-of-one-rule inverted: one name for two rules, so tuning either moved the other.
#: One reported throttle therefore costs this many attempts, and the give-up fires after that
#: many reports. Also renamed from RPC_RETRIES_PER_CALL, since every call uses it now.
RPC_RETRIES_PER_CALL = 3

#: Seconds to wait before retrying one throttled CALL, doubling each time. SECONDS because it
#: is passed to sleep -- an interface, not a report (rule 6).
#:
#: RENAMED FROM RPC_BACKOFF_SECONDS on 2026-09-30, when the retry it governs was extracted
#: for find_a_holder() to share. A name that says MEMO_HUNT while governing every call is the
#: drift rule 8 is about: the next reader tunes it believing it only affects the hunt.
#:
#: RETRY, WHICH THE FIXED PACE ABOVE DOES NOT DO AND IS WHY IT WAS NOT ENOUGH. The pace was
#: added on 2026-09-29 after an earlier 429 storm, and it was a patch rather than a fix
#: (rule 19): it slows every read whether or not the endpoint is complaining, and does nothing
#: at all once one does. A 429 is the one HTTP status where asking again shortly is the correct
#: response, so that is what happens -- and the give-up above is what stops it being infinite.
RPC_BACKOFF_SECONDS = 1.0

#: How long to wait between the hunt's getTransaction calls. SECONDS, because it is passed
#: straight to sleep -- an interface, not a report (rule 6). Measured 2026-09-29: an unpaced
#: hunt of 20 signatures against api.devnet.solana.com got HTTP 429 on 11 of them and on ALL
#: 20 for the second program id, so the run established nothing about that id at all.
MEMO_HUNT_PACING_SECONDS = 0.35


#: The transaction version the HUNT asks for, and it is deliberately NOT the 0 that
#: chains/solana.py's deposit reader uses.
#:
#: MEASURED ON THE OPERATOR'S 2026-09-30 RE-RUN: one of the fifty v2 reads came back
#: `-32015 Transaction version (1) is not supported by the requesting client`, so a real memo
#: was on the cluster and this hunt could not see it. Versioned transactions are ordinary on
#: Solana now, so that is a read lost on every run, not a curiosity.
#:
#: WHY THE CREDIT PATH STAYS AT 0 AND THIS DOES NOT, which is the whole reason this is a
#: separate constant rather than a change to one shared number. chains/solana.py pins 0 because
#: `_native_credits` maps a deposit address to a balance index through `message.accountKeys`
#: ALONE, and a versioned transaction may draw account keys from an ADDRESS LOOKUP TABLE, which
#: arrive in `meta.loadedAddresses` instead -- grepped 2026-09-29, zero hits in the tree, so
#: nothing here knows about them. An address that arrives that way is simply absent from
#: `accountKeys` and a real deposit is silently not credited. That reasoning is sound and it is
#: about the CREDIT path.
#:
#: It does not reach the memo hunt. Checked by AST 2026-09-30 rather than by reading:
#: chains/solana_memo.py touches `transaction.message.instructions`,
#: `meta.innerInstructions`, `programId` and `parsed`, and the strings `accountKeys` and
#: `loadedAddresses` do not appear in that module at all. So there is no index to misalign --
#: the parser matches on a program id carried by the instruction.
#:
#: WHAT IS STILL UNTESTED, AND IT IS A REAL LIMIT (rule 17): whether jsonParsed populates
#: `programId` for a program invoked through an address lookup table. If it does not, such a
#: memo is MISSED rather than misread -- the hunt reads the transaction and reports no memo,
#: which is the same safe direction the parser already takes everywhere else. Nothing here can
#: settle that; the operator's next run can, and a memo count that rises is the evidence.
MEMO_HUNT_TRANSACTION_VERSION = 1


def call_with_backoff(adapter: RpcCaller, method: str, *params):
    """One RPC call, retrying a rate limit, returning (result, throttled, reason).

    EXTRACTED FROM read_one_transaction() 2026-09-30, AND THE OPERATOR'S RUN IS WHY. That
    function had the retry and the throttle/finding split welded to `getTransaction`, so
    find_a_holder() -- written an hour later -- had neither. Its first real run answered

        that lookup FAILED: SolanaRPCError: getTokenLargestAccounts returned HTTP 429
        the field names in find_a_holder() were written from documentation and never measured
        -- this is the finding, not a crash.

    Both lines were wrong together. It did not retry a rate limit that retrying fixes, and then
    it blamed unmeasured field names for the endpoint refusing to answer -- which is the exact
    defect ("a throttled hunt is not a finding") fixed in the memo hunt earlier the same day and
    not carried across. Rule 8: two copies of one rule, and the second copy did not exist yet
    when the first was written, so nothing pointed from one to the other. One copy now.

    EXACTLY ONE OF THE THREE RETURN SHAPES IS MEANINGFUL:

        (result, False, "")       the call answered
        (None, True, reason)      the ENDPOINT refused -- says nothing about our field names
        (None, False, reason)     we asked and could not use what came back -- a real finding

    THE STATUS COMES OFF THE EXCEPTION, not out of its message. chains/solana.SolanaRPCError
    carries `status_code` and `throttled`; sniffing the sentence for "429" would be parsing prose
    that a later reword silently turns into "nothing is ever throttled".
    """
    delay = RPC_BACKOFF_SECONDS
    for attempt in range(1, RPC_RETRIES_PER_CALL + 1):
        try:
            return adapter.call(method, *params), False, ""
        except SolanaRPCError as exc:
            if not exc.throttled:
                return None, False, f"{type(exc).__name__}: {exc}"
            if attempt == RPC_RETRIES_PER_CALL:
                return None, True, f"HTTP 429 after {attempt} attempt(s)"
            time.sleep(delay)
            delay *= 2
        except Exception as exc:  # noqa: BLE001 -- checked: a diagnostic, and the failure is RETURNED as the reason rather than swallowed, so the caller can tell it from a throttle and from a real answer
            return None, False, f"{type(exc).__name__}: {exc}"
    return None, True, "HTTP 429"


def read_one_transaction(adapter: RpcCaller, signature: str):
    """One getTransaction, retrying a rate limit and giving the two failures separate names.

    RETURNS (transaction, throttled, reason). Exactly one of the three is meaningful:

        (dict, False, "")       read it
        (None, True,  reason)   the endpoint refused to answer -- says nothing about the data
        (None, False, reason)   we asked and could not use what came back -- a real finding

    THE SPLIT IS THE POINT AND IT IS RULE 12's BLE001 COMPLAINT ONE LEVEL UP. Until 2026-09-30
    both came back as "could not be read", so an operator's screen showed forty rate limits in
    the same shape as an unparseable transaction -- and only the second is evidence about
    chains/solana.py. The consequence was not cosmetic: the two Memo program ids came out of the
    same run looking alike, one CONFIRMED off ten good reads and the other with nothing read at
    all, and telling those two apart is the entire reason the hunt exists.

    THE STATUS COMES OFF THE EXCEPTION, not out of its message. chains/solana.SolanaRPCError
    carries `status_code` and `throttled`; sniffing the sentence for "429" would be parsing
    prose that a later reword silently turns into "nothing is ever throttled".
    """
    return call_with_backoff(
        adapter, "getTransaction", signature,
        {"encoding": "jsonParsed",
         "maxSupportedTransactionVersion": MEMO_HUNT_TRANSACTION_VERSION})


class OneIdResult(NamedTuple):
    """What reading one program id's traffic produced. Counts, not conclusions.

    The verdict is what_the_hunt_established()'s job; this is the evidence it reads. Kept
    apart so the counting can be exercised without the sentences and the sentences without a
    network (rule 10 -- the decision is the smallest testable piece, and it is not this).
    """

    seen: int
    read: int
    unread: int
    throttled: int
    asked: int
    of: int
    abandoned: bool
    parsed_shape: str


def hunt_one_program_id(adapter: RpcCaller, program: str, entries: list, how_many: int) -> OneIdResult:
    """Read up to `how_many` of one Memo program id's transactions, printing as it goes.

    EXTRACTED FROM hunt_memo() 2026-09-30, because adding the throttle handling put that
    function at C901 13 against a ceiling of 10 -- and CLAUDE.md rule 12 is explicit about
    which way that gets resolved: "a main() past the ceiling is orchestration that has
    swallowed decisions ... the fix is to extract the decision so it can be called with seeded
    inputs, not to raise the ceiling." hunt_memo() is now the loop over the two ids and this is
    one id's read.

    WHAT IT DECIDES, and it is the thing the operator's 2026-09-30 run showed missing: when to
    STOP. Forty consecutive HTTP 429s were asked for anyway, one six-line error block printed
    for each, and then twenty-two more thrown at the second id before Ctrl-C. Three in a row
    ends it now, with a line saying how many were skipped and that the endpoint -- not the
    program id -- is what went quiet.
    """
    seen, unread, throttled, parsed_shape = 0, 0, 0, "(none read)"
    asked, in_a_row, abandoned = 0, 0, False
    of = min(len(entries), how_many)
    for entry in entries[:how_many]:
        signature = entry.get("signature")
        if not signature:
            continue
        # PACED BETWEEN READS, and BACKED OFF on a refusal -- see read_one_transaction(). The
        # pace alone was a patch: it slowed every read whether or not the endpoint was
        # complaining and did nothing once one did. SECONDS here, not microfortnights: an
        # argument to sleep is an interface rather than a report (rule 6).
        time.sleep(MEMO_HUNT_PACING_SECONDS)
        asked += 1
        transaction, was_throttled, reason = read_one_transaction(adapter, signature)
        if transaction is None:
            if not was_throttled:
                unread += 1
                in_a_row = 0
                print(f"    {signature[:16]}...  could not be read: {reason}", flush=True)
                continue
            throttled += 1
            in_a_row += 1
            # ONE LINE FOR THE FIRST, A COUNT FOR THE REST. The operator's run printed a
            # six-line error block forty times over and then twenty-two more for the second
            # id, which buried the one real result in the middle of it. Rule 14 says silence
            # is a defect; forty copies of one sentence is the same defect from the other
            # side -- the screen says nothing a reader can act on either way.
            if in_a_row == 1:
                print(f"    {signature[:16]}...  endpoint THROTTLED us ({reason}); "
                      f"backing off and retrying", flush=True)
            if in_a_row >= MEMO_HUNT_GIVE_UP_AFTER_THROTTLES:
                abandoned = True
                print(f"    ABANDONED this id after {in_a_row} throttled reads in a row (asked "
                      f"{asked} of {of}). Not asking the remaining {of - asked}; the endpoint "
                      f"has stopped answering, which says nothing about the program id.",
                      flush=True)
                break
            continue
        in_a_row = 0
        instructions = ((transaction or {}).get("transaction", {})
                        .get("message", {}) or {}).get("instructions") or []
        parsed_shape = ("jsonParsed (parsed present)"
                        if any(isinstance(i, dict) and "parsed" in i for i in instructions)
                        else "NOT jsonParsed -- instructions came back encoded")
        memos = memo_strings_in(transaction)
        if memos:
            seen += len(memos)
            tag, why = deposit_tag_from(transaction)
            print(f"    {signature[:16]}...  {len(memos)} memo(s)  "
                  f"tag={tag if tag is not None else 'none'}  <- {why}", flush=True)
    # THE DENOMINATOR, because a zero above is ambiguous without it: no memo found over twenty
    # transactions read is a different fact from no memo found over twenty that could not be
    # read at all (rule 3 -- state what it was counted out of). THROTTLED IS ITS OWN COLUMN: it
    # is neither a read nor a defect in what came back, and adding it to `unread` is what made
    # forty refusals look like forty bad transactions.
    read = asked - unread - throttled
    print(f"    asked {asked} of {of}; read {read}, throttled {throttled}, "
          f"unreadable {unread}; encoding received: {parsed_shape}"
          + ("  <- ABANDONED EARLY" if abandoned else ""), flush=True)
    return OneIdResult(seen=seen, read=read, unread=unread, throttled=throttled,
                       asked=asked, of=of, abandoned=abandoned, parsed_shape=parsed_shape)


def hunt_memo(adapter: SolanaAdapter, how_many: int) -> bool:
    """Read real Memo-program transactions and check our parser recognizes them. Read-only.

    WHY THIS EXISTS, AND WHY IT IS NOT A TEST. `chains/solana_memo.py` names two program ids
    that were WRITTEN rather than measured -- nothing in the container they were written in can
    reach a Solana cluster. Its header says so per id, and a test asserts each one's status.
    ONE OF THE TWO IS NOW SETTLED: the operator's 2026-09-30 run read ten of v2's own
    transactions and found a memo in all ten. v1 is what is left, and it is still worth having
    -- older wallets still emit it, so a wrong id there means a deposit that parses to no memo
    and money sitting uncredited while everything reports success.
    But an admission is not a measurement, and the failure it is admitting to is specific: if
    the id is wrong, `memo_strings_in()` returns nothing on every real deposit, and a zero
    match rate reads as "nobody uses memos" rather than as "the constant is wrong". Both look
    like silence.

    So this asks the cluster. It reads recent signatures FOR THE MEMO PROGRAM ACCOUNT, pulls
    those transactions, and runs the real parser over them. Somebody else's memo proves the id
    as well as our own would -- the same principle as xrp_chain_check.py's --hunt-tag, which
    settled the DestinationTag spelling off a stranger's payment.

    NOT FOLDED INTO THE EXIT CODE, deliberately. Finding no memo traffic says something about
    the cluster, not about our code, and `print_summary`'s failures are for "a method or field
    the adapter depends on did not match a real server". A cluster with no recent memos must not
    read as a broken adapter. What it returns is whether the id was CONFIRMED, so a caller can
    print the difference.

    THE ENCODING IS REPORTED RATHER THAN ASSUMED. `memo_strings_in()` reads the jsonParsed
    shape only, and a cluster that answers with base58-encoded instructions would produce zero
    memos for a reason that has nothing to do with the program id. This prints which encoding
    came back, so those two are never confused.
    """
    print(flush=True)
    print(f"MEMO PROGRAM  (reading up to {how_many} recent transaction(s) of the Memo program "
          f"itself; read-only, sends nothing)", flush=True)
    print("  WHY: chains/solana_memo.py names two program ids. If one is wrong, a deposit that", flush=True)
    print("  used it parses to no memo -- which looks exactly like a cluster nobody sends memos", flush=True)
    print("  on. This tells those two apart, off other people's memo traffic.", flush=True)
    # THE STATUS PER ID, DERIVED, so this banner cannot drift from the constants the way four
    # other surfaces drifted from the custody decision earlier on 2026-09-30. A hand-written
    # "v2 is measured" here would be a fifth copy of a fact that lives in solana_memo.py.
    for program in MEMO_PROGRAM_IDS:
        known = "MEASURED 2026-09-30" if program in MEASURED_MEMO_PROGRAM_IDS else "NOT YET MEASURED"
        print(f"  {program}  <- {known}", flush=True)

    confirmed = False
    for program in MEMO_PROGRAM_IDS:
        done = step(f"getSignaturesForAddress({program[:12]}...)", "recent memo-program traffic")
        try:
            signatures = adapter.call(
                "getSignaturesForAddress", program, {"limit": max(1, min(how_many, 1000))})
        except Exception as exc:  # noqa: BLE001 -- checked: a diagnostic. The failure is named and printed, and this hunt is deliberately outside the exit code
            done(f"{type(exc).__name__}: {exc}", ok=False)
            continue
        entries = list(signatures or [])
        done(f"{len(entries)} signature(s)")
        if not entries:
            print("    (none) -- no recent traffic for this id. That is NOT evidence the id is", flush=True)
            print("    wrong, and NOT evidence it is right: this cluster may simply be quiet.", flush=True)
            continue
        outcome = hunt_one_program_id(adapter, program, entries, how_many)
        established, lines = what_the_hunt_established(
            program, seen=outcome.seen, read=outcome.read, unread=outcome.unread,
            throttled=outcome.throttled)
        confirmed = confirmed or established
        for line in lines:
            print(line, flush=True)
    if not confirmed:
        print("  NOT CONFIRMED: no memo was read back. The ids in chains/solana_memo.py remain", flush=True)
        print("  WRITTEN RATHER THAN MEASURED, exactly as that file says.", flush=True)
    return confirmed


def _network_line(adapter: RpcCaller) -> str:
    genesis = str(adapter.call("getGenesisHash"))
    # solana_cluster() rather than .get() with the sentence inline: swap_readiness.py
    # renders the same verdict, and a default spelled at two call sites is two
    # spellings of one answer (rule 8).
    return f"{solana_cluster(genesis)}  (genesis {genesis})"


def _epoch_line(adapter: SolanaAdapter) -> str:
    info = adapter.call("getEpochInfo", {"commitment": adapter.commitment})
    return f"epoch={info.get('epoch')} slotIndex={info.get('slotIndex')} absoluteSlot={info.get('absoluteSlot')}"


def _rent_line(adapter: SolanaAdapter, space: int, expected: int) -> str:
    """One rent reading, said so the operator does not have to carry it back.

    WHAT THIS LINE USED TO LEAVE OUT. It read "DIFFERS from the reference N;
    the chain is the authority, the constant is not" -- true, and unactionable:
    it named neither the cluster the reference was for nor what a difference
    would mean, so the 2026-09-26 devnet mismatch (650240 against a reference of
    890880) read as possibly-normal-for-devnet when in fact the reference was
    stale on every cluster. A mismatch now prints the implied lamports/byte,
    which is the ONE parameter that moves (SIMD-0437's five steps) and is
    therefore the number that identifies what happened.
    """
    actual = adapter.rent_exempt_minimum(space)
    if actual == expected:
        return (
            f"{actual} lamports for {space} bytes  <- matches the reference for "
            f"{LAMPORTS_PER_BYTE_FOR_RENT_EXEMPTION} lamports/byte"
        )
    overhead = ACCOUNT_STORAGE_OVERHEAD_BYTES + space
    implied = actual / overhead
    implied_text = f"{int(implied)}" if actual % overhead == 0 else f"{implied:.2f} (not a whole number -- so the 128-byte overhead assumption is what to doubt first)"
    return (
        f"{actual} lamports for {space} bytes  <- DIFFERS from the reference {expected}, which is "
        f"{LAMPORTS_PER_BYTE_FOR_RENT_EXEMPTION} lamports/byte: MEASURED on devnet 2026-09-26, and for mainnet-beta "
        "and testnet sourced from solana.com/upgrades/reduced-rent rather than measured. "
        f"This reading implies {implied_text} lamports/byte. THE CHAIN IS THE AUTHORITY and nothing sizes a "
        "transfer from the constant. If the implied figure is one of SIMD-0437's steps (6960 -> 6333 -> "
        "5080 -> ... -> 696) that cluster is on a different step and chains/solana_units.py wants the new number; if it is none of those, "
        "this endpoint is not a public Solana cluster."
    )


def _ata_line(adapter: SolanaAdapter, address: str) -> str:
    account = adapter.associated_token_address_for(address)
    exists = adapter.token_account_exists(account)
    note = "exists" if exists else "DOES NOT EXIST -- creating it costs the rent-exempt minimum above, paid by whoever signs"
    return f"{account}  <- {note}"


def _balance_line(adapter: SolanaAdapter, address: str) -> str:
    # get_balance() reads the adapter's configured hot wallet, so an address
    # given on the command line is honored by asking about THAT one. Stated
    # here rather than silently reading a different account than the banner
    # printed.
    probe = SolanaAdapter(
        url=adapter.url, commitment=adapter.commitment, timeout=adapter.timeout,
        mint=adapter.mint, hot_wallet=address, min_commitment_rank=adapter.min_commitment_rank,
    )
    probe.call = adapter.call
    unit = adapter.mint or "SOL"
    return f"{probe.get_balance()} {unit}  <- read at finalized commitment; an unsettled balance can go away"


class _CapturedAdapterLogs(logging.Handler):
    """Buffers chains.solana's log records so they land INSIDE the step, not across it.

    WHY A HANDLER AND NOT A SILENCE. The adapter logs a dropped credit at WARNING, which is
    right -- an operator reading a log file later needs it. But it goes to stderr the moment it
    happens, which on the operator's 2026-09-30 run put an unindented 300-character sentence
    between the step's announcement and its result:

        find_deposits_to_address(limit=10) ...  <- THE REAL METHOD ...
    SOL deposit 2K2Pw1Hz... CANNOT BE ATTRIBUTED and was NOT credited: ...
        ok   (none) CREDITED -- but 1 credit(s) ...

    CLAUDE.md's "every diagnostic has to be a single pasteable block" is not a style note here:
    the operator pastes this back, and a line at column 0 in the middle of a step breaks the
    alignment that makes the block readable at a glance.

    SUPPRESSING IT WOULD BE THE WRONG FIX even though the step now reports the same drop in more
    detail. The step reports what it READ; the log is what the ADAPTER said, and a future record
    this script does not know how to summarize would vanish. Buffered and re-emitted indented,
    nothing is lost and the frame holds.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(self.format(record))


#: A base58 run long enough to be a signature or an address. Used to tell records apart from
#: each other by their REASON rather than by which transaction they happened to name.
_BASE58_RUN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,}")


def _one_reason_per_group(records: list[str]) -> list[tuple[str, list[str]]]:
    """Records grouped by what they SAY, ignoring which transaction they name.

    Returns [(the first record of the group, every base58 token the group named)], in the order
    the groups first appeared. Pure, so the grouping is testable without a cluster.

    THE KEY IS THE RECORD WITH ITS SIGNATURES BLANKED. Two warnings that differ only in which
    transaction was throttled are one piece of information repeated; two that differ in their
    REASON are two findings and both have to be read.
    """
    groups: dict[str, tuple[str, list[str]]] = {}
    for record in records:
        key = _BASE58_RUN.sub("<base58>", record)
        first, named = groups.setdefault(key, (record, []))
        named.extend(_BASE58_RUN.findall(record))
        groups[key] = (first, named)
    return list(groups.values())


@contextmanager
def _capturing_adapter_logs():
    """Hold chains.solana's records for the duration of a step, then give them back.

    EXTRACTED 2026-10-01 BECAUSE THE DEFECT RECURRED IN A SECOND PLACE, which is rule 8 exactly
    -- the handler existed, was correct, and only one of the two readers used it. The targeted
    proof step calls `_credits_in_transaction` directly and had no capture, so the operator's
    seventh run printed the adapter's WARNING at column 0 between the step's announcement and
    its result:

        _spl_credits over 5MVQ2U12Y8NcdV7y... ...  <- the one transaction known to CREDIT ...
    SOL deposit 5MVQ2U12Y8NcdV7yD9bc... CANNOT BE ATTRIBUTED and was NOT credited: ...
        ok   DECODED 1 credit(s) and then REFUSED them: ...

    That is the same three lines _CapturedAdapterLogs' own docstring quotes from 2026-09-30, one
    step over. The addHandler/propagate/finally dance was six lines inlined in one caller, which
    is how the second caller came to not have it; as a context manager there is one spelling and
    adding a third reader cannot forget it.

    `propagate = False` for the duration, restored in the finally: without it the record reaches
    the root handler as well and prints raw, which is the defect above. The restore is in a
    finally so an exception cannot leave the adapter's logger detached from the root for the
    rest of the process.
    """
    captured = _CapturedAdapterLogs()
    adapter_logger = logging.getLogger("chains.solana")
    adapter_logger.addHandler(captured)
    was_propagating = adapter_logger.propagate
    adapter_logger.propagate = False
    try:
        yield captured
    finally:
        adapter_logger.removeHandler(captured)
        adapter_logger.propagate = was_propagating


def _indented(records: list[str], indent: str = "      ",
              already_listed: frozenset[str] = frozenset()) -> str:
    """Log records folded into a step's output, ONE PER DISTINCT REASON, aligned.

    COLLAPSED BY REASON, AND THE OPERATOR'S 2026-10-01 RUN IS WHY. `--limit 50` against public
    devnet got 42 of 50 transactions throttled, and this printed all 42 warnings in full: forty-
    two six-line blocks, each ~300 characters, each saying the identical thing about a different
    signature. The one line that mattered -- "read 8 of 50 listed" -- was at the bottom of it.

    THAT IS THE SAME DEFECT AS THE MEMO HUNT'S, in a second place. Rule 14 says silence is a
    defect; this is the same defect from the other side, which that hunt already fixed once with
    "one line for the first, a count for the rest" and which did not carry across to the
    adapter's own warnings. Third time one of these lessons has had to be applied twice.

    EVERY SIGNATURE IS STILL PRINTED, compactly, because they are what an operator needs to go
    and recover a deposit by hand -- dropping them to shorten the block would trade one
    unusable output for another. What is removed is the repetition of the REASON, not the
    evidence.

    `already_listed` IS HOW THAT JUSTIFICATION GETS CHECKED RATHER THAN ASSUMED. It holds the
    signatures the calling step has already printed above this block, and those are elided here
    with a count instead. The 2026-10-01 run is why: the stranded-deposit step listed both
    signatures with their reason, and then this block printed one of them again in full and the
    other in a "for recovery by hand" list -- two facts, four 88-character base58 strings, and
    nothing a reader could do with the second copy. "Recovery by hand" is a real need and is
    exactly what the step's own list serves; repeating it is not a second kind of evidence.

    The set is a PARAMETER rather than read from the adapter, because this helper also folds in
    warnings for steps that print no list of their own (the no-credit branches) -- there the
    default empty set keeps every signature, which is the behavior those branches need.
    """
    if not records:
        return ""
    lines = []
    for first, named in _one_reason_per_group(records):
        head, *rest = first.splitlines() or [""]
        lines.append(f"\n{indent}logged: {head}")
        lines.extend(f"\n{indent}        {line}" for line in rest)
        in_the_head = _BASE58_RUN.findall(first)
        extra = [token for token in named if token not in in_the_head]
        if not extra:
            continue
        unlisted = [token for token in extra if token not in already_listed]
        if not unlisted:
            lines.append(f"\n{indent}        ... and the SAME reason for {len(extra)} more, "
                         f"every one already listed above.")
            continue
        # "NOT already listed above" ONLY WHEN SOME WERE, because with an empty already_listed
        # -- the no-credit branches, which print no list of their own -- that phrasing describes
        # a list the reader never saw and makes them go looking for it.
        qualified = "Every one" if len(unlisted) == len(extra) else "Every one not listed above"
        lines.append(f"\n{indent}        ... and the SAME reason for {len(extra)} more. "
                     f"{qualified}, for recovery by hand:")
        lines.append(f"\n{indent}          " + " ".join(unlisted))
    return "".join(lines)


def _deposits_line(adapter: DepositReader, address: str, limit: int,
                   credited: list[int] | None = None) -> str:
    """What the deposit watcher would see, including what it would REFUSE to credit.

    THE LINE THIS REPLACES WAS FALSE ON THE FIRST RUN THAT REACHED IT. On 2026-09-30 a real
    devnet credit was read, correctly refused for carrying no memo, and this printed

        (none)  <- zero credits in the signatures read. This is a RESULT, not a failure.

    four lines below its own WARNING saying one credit had been dropped. Zero credits were not
    read; one was, and discarded. The two are different operator situations and the difference is
    money: "nothing arrived" is normal, and "something arrived that nobody can claim" is a
    support ticket with somebody's deposit in it.

    THREE OUTCOMES, ALL NAMED, because an empty return value covers all three (rule 14):

        no signatures at all        nothing has touched this account in the window
        signatures, no credits      transactions exist; none of them credited this address
        credits, all refused        MONEY IS STRANDED. This is the one that must never render
                                    as "(none)"
    """
    # CAPTURED FOR THE DURATION OF THE CALL ONLY, and removed in a finally so an exception
    # cannot leave a handler attached to the adapter's logger for the rest of the process.
    with _capturing_adapter_logs() as captured:
        events = adapter.find_deposits_to_address(address, tx_limit=limit)
    dropped = adapter.unattributable_drops
    if credited is not None:
        # HOW MANY CREDITS THIS CALL PRODUCED, handed back so check_address can report coverage
        # from the run rather than from the flags. A list rather than a return value because
        # this function's return value is the line an operator reads, and widening it to a tuple
        # would put a number into the middle of the output plumbing.
        credited.append(len(events))
    if dropped:
        # REPORTED FIRST AND AS A PROBLEM, not appended to a "(none)". The credits are real.
        #
        # AND WITH ITS COVERAGE, since 2026-10-01. This branch printed no denominator at all,
        # so the operator's run showed `find_deposits_to_address(limit=5)` on the step line and
        # `over 4 signature(s)` in the summary with nothing reconciling them -- 4 listed, or 5
        # listed and one unfetched? The adapter knows (signatures_listed, unreadable_signatures)
        # and this was the one branch that did not ask. It is also the branch where it matters
        # most: a stranded credit is already proven here, so "a deposit in the unfetched ones is
        # not ruled out" is a live possibility rather than a caveat.
        lines = [
            f"(none) CREDITED -- but {sum(d.credits for d in dropped)} credit(s) across "
            f"{len(dropped)} transaction(s) WERE READ AND REFUSED. Real money arrived that no "
            f"swap can claim; matching it is a human's job.",
            # ON ITS OWN INDENTED LINE, matching the per-drop lines below it. Appended to the
            # sentence above, it made a ~300-character wall that the operator's terminal wrapped
            # mid-clause, in a block where every other element gets its own line.
            f"\n      {coverage_clause(adapter, limit)}",
        ]
        lines.extend(
            # THE AMOUNT, now that UnattributableCredit carries one. This line listed the
            # signature and the reason and not the number -- which is the thing an operator
            # needs first to decide whether to go looking, and the reason the field was added.
            # Named as a delta at the point of print, because the summary's `balance` step a few
            # lines up shows a different quantity in the same unit (see the adapter's warning).
            f"\n      {d.signature}\n        {d.amount} credited by this transaction and "
            f"DROPPED ({d.credits} credit(s)): {d.why}"
            for d in dropped
        )
        # THE ADAPTER'S OWN WARNING, folded in rather than left to cross the block. The
        # caveat that used to be here -- "the WARNING above this step is the same event" --
        # was a note explaining a formatting defect instead of fixing it.
        # THE SIGNATURES ARE ALREADY ABOVE, every one of them: the loop just above lists each
        # drop with its reason. Handing that set to _indented() elides the second copy -- see
        # its docstring for what your 2026-10-01 run printed without this.
        lines.append(_indented(captured.records,
                               already_listed=frozenset(d.signature for d in dropped)))
        return "".join(lines)
    if not events and not adapter.signatures_listed:
        # LISTED, NOT READ, and that is a fix rather than a rename. This tested
        # `signatures_read`, which is listed MINUS the ones getTransaction could not fetch -- so
        # an account with three signatures whose every getTransaction was throttled came out at
        # read=0 and printed "nothing has touched this account in the window". Three things had
        # touched it and none of them could be read, which is the opposite conclusion and the
        # one that hides a deposit. Found 2026-10-01 while giving the branch below its
        # denominator; on a rate-limited public endpoint, all-of-a-small-window throttling is
        # not a hypothetical.
        return ("(none)  <- and ZERO signatures were LISTED for it, so nothing has touched this "
                "account in the window. A RESULT, not a failure." + _indented(captured.records))
    if not events:
        # "NO CREDIT TO THIS ADDRESS", NOT "NONE OF THEM". The pronoun had no antecedent: this
        # read "none of them credited this address" where `them` pointed FORWARD to a count in
        # the next sentence. It was fine before the count moved into coverage_clause() in
        # 6f6a69d -- "9 signature(s) FETCHED and none credited this address" -- and factoring
        # the clause out broke the sentence that used to carry it. Spotted in the operator's
        # paste, which is where output defects become visible (rule 14).
        return (f"(none)  <- no credit to this address in the signatures read. "
                f"{coverage_clause(adapter, limit)} A RESULT, not a failure."
                + _indented(captured.records))
    lines = [f"{len(events)} credit(s):"]
    lines.extend(
        f"\n      {event['txid']}\n        vout={event['vout']} (account index, read from the tx -- never fabricated) "
        f"amount={event['amount']}\n        {describe_commitment(event['confirmations'], adapter.min_commitment_rank)}"
        for event in events
    )
    return "".join(lines)


def coverage_clause(adapter: SignatureCoverage, limit: int) -> str:
    """How much of the window was actually read, as one sentence. ONE COPY, used by two branches.

    IT EXISTED IN ONLY ONE OF THEM UNTIL 2026-10-01, and the missing one was the branch that
    reports stranded money -- see the comment at the `if dropped:` above. Written as a shared
    function rather than copied, because the two would have read the same on the day they were
    written and drifted from then on (rule 8), and the drift here is a denominator: rule 3's
    "a count without what it was counted out of has caused real errors here more than once".

    THREE NUMBERS AND THEY ARE NOT THE SAME NUMBER, which is the whole reason this is a sentence
    and not a count:

        limit                      what the run ASKED for
        signatures_listed          what getSignaturesForAddress returned. Fewer than the limit
                                   means the endpoint had no more to give, not an error.
        signatures_read            listed minus the ones getTransaction could not fetch. On a
                                   rate-limited endpoint this is routinely lower, and a deposit
                                   inside an unfetched transaction is neither credited nor
                                   ruled out.

    The operator reads the screen, not the source (rule 14), so all three appear whenever they
    disagree and the sentence says what the gap means.
    """
    listed, read = adapter.signatures_listed, adapter.signatures_read
    asked = "" if listed == limit else f" (the limit asked for {limit})"
    if read == listed:
        return f"All {listed} listed signature(s) were FETCHED{asked}, so the window was read in full."
    return (f"{read} of {listed} listed signature(s) were fetched{asked}; "
            f"{listed - read} could NOT be fetched -- a deposit inside those is NOT credited "
            f"and is NOT ruled out.")


if __name__ == "__main__":
    raise SystemExit(main())
