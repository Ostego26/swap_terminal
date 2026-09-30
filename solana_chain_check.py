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
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_ROOT = REPO_ROOT / "swap_terminal"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# All five imports are reachable only because of the sys.path line above;
# hoisting them would break the import it enables. That is what the E402
# suppressions claim and what a reader can check from these lines.
from chains.solana import SolanaAdapter, SolanaRPCError  # noqa: E402
from chains.solana_address import describe_address  # noqa: E402
from chains.solana_memo import MEMO_PROGRAM_IDS, deposit_tag_from, memo_strings_in  # noqa: E402
from chains.solana_units import (  # noqa: E402
    ACCOUNT_STORAGE_OVERHEAD_BYTES,
    LAMPORTS_PER_BYTE_FOR_RENT_EXEMPTION,
    RENT_EXEMPT_SYSTEM_ACCOUNT_LAMPORTS,
    RENT_EXEMPT_TOKEN_ACCOUNT_LAMPORTS,
    SYSTEM_ACCOUNT_SPACE,
    TOKEN_ACCOUNT_SPACE,
    describe_commitment,
)
from config import Config  # noqa: E402
from microfortnights import format_duration  # noqa: E402

# The genesis hashes that identify Solana's three public clusters. A cluster
# cannot lie about this and it does not depend on the URL's hostname, which is
# why the network is IDENTIFIED rather than inferred from the endpoint: an
# operator pointing a "devnet" alias at mainnet would otherwise read the word
# devnet in this banner all the way to a mainnet transfer.
GENESIS_HASHES = {
    "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d": "MAINNET-BETA  <- REAL MONEY",
    "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG": "DEVNET",
    "4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY": "TESTNET",
}


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


def print_banner(rpc: dict, address: str) -> None:
    """Everything that decides the answer, before anything runs (rule 14)."""
    print("solana_chain_check: READ-ONLY. It signs nothing and broadcasts nothing.", flush=True)
    print(f"  endpoint        {rpc['url'] or '(SOL_RPC_URL is UNSET -- nothing can be checked)'}", flush=True)
    print(f"  mint            {rpc['mint'] or '(none -- checking native SOL)'}", flush=True)
    print(f"  address         {address or '(none given; pass --address or set SOL_HOT_WALLET)'}", flush=True)
    print(f"  threshold       rank {rpc['min_commitment_rank']}  <- a RUNG on the commitment ladder, NOT blocks", flush=True)
    print(f"  database        {Config.DB_PATH}  <- echoed for the paste; this script does not open it", flush=True)


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


def check_address(adapter: SolanaAdapter, address: str, limit: int, run) -> None:
    if not address:
        print("\nADDRESS  (none given -- pass --address or set SOL_HOT_WALLET to check one)", flush=True)
        return
    print(f"\nADDRESS  {address}", flush=True)
    print(f"    {describe_address(address)}", flush=True)
    if adapter.is_spl:
        run("associated token account", "derived locally; where an SPL balance for this owner actually lives",
            lambda: _ata_line(adapter, address))
    run("balance", "the token account's balance for an SPL mint, or lamports for native SOL",
        lambda: _balance_line(adapter, address))
    run(f"find_deposits_to_address(limit={limit})", "THE REAL METHOD the deposit watcher calls. '(none)' is a result.",
        lambda: _deposits_line(adapter, address, limit))


def print_summary(failures: list[str], elapsed: float) -> int:
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
    print("  NOT PROVEN by this run: nothing was sent. get_new_address() and send_to_address() refuse by design,", flush=True)
    print("  and the deposit-address strategy is still the operator's choice (README.md, 'Solana deposit addresses').", flush=True)
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
        "--hunt-memo", type=int, default=0, metavar="N",
        help="also read N recent transactions from the Memo program itself and check that "
             "chains/solana_memo.py recognizes them (read-only; settles the program id "
             "without sending anything)",
    )
    args = parser.parse_args()

    rpc = dict(Config.RPC["SOL"])
    if args.mint:
        rpc["mint"] = args.mint
    address = args.address or rpc.get("hot_wallet") or ""

    started = time.monotonic()
    print_banner(rpc, address)
    if not rpc["url"]:
        print("  FAIL  SOL_RPC_URL is unset, so there is nothing to check. Set it and run again.", flush=True)
        return 1

    adapter = SolanaAdapter(**rpc)
    failures: list[str] = []
    run = make_runner(failures)
    check_cluster(adapter, run)
    check_rent(adapter, run)
    check_mint(adapter, run)
    check_address(adapter, address, args.limit, run)
    if args.hunt_memo > 0:
        hunt_memo(adapter, args.hunt_memo)
    return print_summary(failures, time.monotonic() - started)


def what_the_hunt_established(program: str, *, seen: int, read: int, unread: int) -> tuple[bool, list[str]]:
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
        read == 0           NOT ESTABLISHED. Nothing was parsed, so this is evidence
                            about the ENDPOINT, not about the id.
        read > 0, seen == 0 We read transactions and found no memo we recognize. THAT is
                            evidence, and the encoding line says which kind.
    """
    if seen:
        return True, [
            f"    CONFIRMED: {program} is a real Memo program id and",
            "    chains/solana_memo.memo_strings_in() reads its instructions.",
        ]
    if read == 0:
        return False, [
            f"    NOT ESTABLISHED: all {unread} transaction(s) were unreadable, so NOTHING was",
            "    parsed and this says nothing about the program id -- neither that it is right",
            "    nor that it is wrong. The reasons are printed above; HTTP 429 means the",
            "    endpoint throttled us, not that the id is bad. Re-run with a smaller N, or",
            "    against an endpoint that is not rate-limited.",
        ]
    return False, [
        f"    read {read} transaction(s) for this id and found NO memo our parser recognizes.",
        "    If the encoding line above says NOT jsonParsed, that is the cause and the program",
        "    id is still unsettled. If it says jsonParsed, the id is wrong.",
    ]


#: How long to wait between the hunt's getTransaction calls. SECONDS, because it is passed
#: straight to sleep -- an interface, not a report (rule 6). Measured 2026-09-29: an unpaced
#: hunt of 20 signatures against api.devnet.solana.com got HTTP 429 on 11 of them and on ALL
#: 20 for the second program id, so the run established nothing about that id at all.
MEMO_HUNT_PACING_SECONDS = 0.35


def hunt_memo(adapter: SolanaAdapter, how_many: int) -> bool:
    """Read real Memo-program transactions and check our parser recognizes them. Read-only.

    WHY THIS EXISTS, AND WHY IT IS NOT A TEST. `chains/solana_memo.py` names two program ids
    that were WRITTEN rather than measured -- nothing in the container they were written in can
    reach a Solana cluster. Its header says so, and a test asserts the admission is still there.
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
    print("  WHY: chains/solana_memo.py names two program ids that were WRITTEN, not measured.", flush=True)
    print("  If the id is wrong, every real deposit parses to no memo -- which looks exactly", flush=True)
    print("  like a cluster nobody sends memos on. This tells those two apart.", flush=True)

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
        seen, unread, parsed_shape = 0, 0, "(none read)"
        for entry in entries[:how_many]:
            signature = entry.get("signature")
            if not signature:
                continue
            # PACED, because the public devnet endpoint throttled 11 of 20 reads on
            # 2026-09-29 and a hunt that cannot finish settles nothing. SECONDS here, not
            # microfortnights: this is an argument to sleep, which is an interface rather
            # than a report (rule 6). The figure is deliberately small -- it costs a few
            # seconds over a 20-signature hunt and turns a 429 storm into a complete answer.
            time.sleep(MEMO_HUNT_PACING_SECONDS)
            try:
                transaction = adapter.call(
                    "getTransaction", signature,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0})
            except Exception as exc:  # noqa: BLE001 -- checked: one unreadable transaction must not end the hunt, and it is REPORTED rather than skipped in silence -- an unread transaction and a memo-less one must not both show up as nothing
                unread += 1
                print(f"    {signature[:16]}...  could not be read: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                continue
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
        # THE DENOMINATOR, because a zero above is ambiguous without it: no memo found over
        # twenty transactions read is a different fact from no memo found over twenty that
        # could not be read at all (rule 3 -- state what it was counted out of).
        read = min(len(entries), how_many) - unread
        print(f"    read {read} transaction(s), "
              f"{unread} unreadable; encoding received: {parsed_shape}", flush=True)
        established, lines = what_the_hunt_established(program, seen=seen, read=read, unread=unread)
        confirmed = confirmed or established
        for line in lines:
            print(line, flush=True)
    if not confirmed:
        print("  NOT CONFIRMED: no memo was read back. The ids in chains/solana_memo.py remain", flush=True)
        print("  WRITTEN RATHER THAN MEASURED, exactly as that file says.", flush=True)
    return confirmed


def _network_line(adapter: SolanaAdapter) -> str:
    genesis = str(adapter.call("getGenesisHash"))
    name = GENESIS_HASHES.get(genesis, "UNRECOGNIZED -- a local validator has its own genesis, so this is expected for solana-test-validator")
    return f"{name}  (genesis {genesis})"


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


def _deposits_line(adapter: SolanaAdapter, address: str, limit: int) -> str:
    events = adapter.find_deposits_to_address(address, tx_limit=limit)
    if not events:
        return "(none)  <- zero credits in the signatures read. This is a RESULT, not a failure."
    lines = [f"{len(events)} credit(s):"]
    lines.extend(
        f"\n      {event['txid']}\n        vout={event['vout']} (account index, read from the tx -- never fabricated) "
        f"amount={event['amount']}\n        {describe_commitment(event['confirmations'], adapter.min_commitment_rank)}"
        for event in events
    )
    return "".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
