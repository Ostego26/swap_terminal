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
from chains.solana_units import (  # noqa: E402
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
    print("\nRENT  (reference constants are NOT measurements -- this is what the chain says)", flush=True)
    run("getMinimumBalanceForRentExemption(0)", f"expected about {RENT_EXEMPT_SYSTEM_ACCOUNT_LAMPORTS} lamports for a system account",
        lambda: _rent_line(adapter, SYSTEM_ACCOUNT_SPACE, RENT_EXEMPT_SYSTEM_ACCOUNT_LAMPORTS))
    run("getMinimumBalanceForRentExemption(165)", f"expected about {RENT_EXEMPT_TOKEN_ACCOUNT_LAMPORTS} lamports for an SPL token account",
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
    return print_summary(failures, time.monotonic() - started)


def _network_line(adapter: SolanaAdapter) -> str:
    genesis = str(adapter.call("getGenesisHash"))
    name = GENESIS_HASHES.get(genesis, "UNRECOGNIZED -- a local validator has its own genesis, so this is expected for solana-test-validator")
    return f"{name}  (genesis {genesis})"


def _epoch_line(adapter: SolanaAdapter) -> str:
    info = adapter.call("getEpochInfo", {"commitment": adapter.commitment})
    return f"epoch={info.get('epoch')} slotIndex={info.get('slotIndex')} absoluteSlot={info.get('absoluteSlot')}"


def _rent_line(adapter: SolanaAdapter, space: int, expected: int) -> str:
    actual = adapter.rent_exempt_minimum(space)
    agrees = "matches" if actual == expected else f"DIFFERS from the reference {expected}"
    return f"{actual} lamports for {space} bytes  <- {agrees}; the chain is the authority, the constant is not"


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
